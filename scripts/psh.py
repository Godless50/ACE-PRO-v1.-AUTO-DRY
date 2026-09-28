"""Выполнить команды на принтере по SSH. Скрипт читается со stdin.

run() возвращает пару (вывод, код_возврата). Код возврата — это код
завершения удалённой команды: SSH-клиент завершается с тем же кодом,
что и команда на другом конце, поэтому child.exitstatus после
child.close() и есть искомое значение. Если соединение оборвалось
ненормально (обрыв дропбира, сигнал), exitstatus будет None — это тоже
нужно считать неуспехом.

Функция намеренно не бросает исключение сама: psh.py — это тонкий
транспорт для разовых команд, а не только для доставки файлов. Часть
вызовов (диагностика, grep по логу) не считает ненулевой код ошибкой.
Тот, кому нужна строгая проверка (например, scripts/deploy_printer.sh),
обязан проверить код сам — так и сделано в deploy_printer.sh.
"""
import base64
import os
import sys
import tempfile

import pexpect

HOST = os.environ.get("PRINTER_HOST", "<printer-ip>")
PASSWORD = os.environ.get("PRINTER_SSH_PASSWORD", "<printer-ssh-password>")


def run(script, timeout=120):
    enc = base64.b64encode(script.encode()).decode()
    cmd = ("ssh -o StrictHostKeyChecking=accept-new "
           "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 "
           "root@%s 'echo %s | base64 -d | sh'" % (HOST, enc))
    child = pexpect.spawn(cmd, timeout=timeout, encoding='utf-8',
                          codec_errors='replace')
    if child.expect(['assword:', pexpect.EOF, pexpect.TIMEOUT]) == 0:
        child.sendline(PASSWORD)
    child.expect(pexpect.EOF, timeout=timeout)
    output = child.before or ""
    child.close()
    return output, child.exitstatus


def put_file(local_path, remote_path, owner='lava:lava', mode='644',
             timeout=300):
    """Залить файл на принтер ОДНОЙ сессией. Вернуть (успех, сообщение).

    Почему не через run(): run() кладёт всю полезную нагрузку в АРГУМЕНТ
    удалённой команды, а dropbear на этом принтере рвёт соединение на
    команде длиннее примерно 7000 символов. Из-за этого файлы приходилось
    лить кусками по 4000 символов, и модуль на 24 КБ занимал девять
    отдельных сессий и около пяти минут — притом что каждая оборванная по
    таймауту попытка оставляла мусор в каталоге модулей.

    Ограничение длины есть только у командной строки. У стандартного ввода
    его нет, поэтому здесь данные идут именно туда, а в команде остаётся
    лишь путь и ожидаемая сумма. Замер на живом принтере: те же 24 КБ за
    25 секунд одной сессией.

    Пароль по-прежнему читается с псевдотерминала, который создаёт pexpect,
    поэтому перенаправление стандартного ввода ему не мешает.

    Проверка и подмена делаются НА ПРИНТЕРЕ в той же сессии: файл
    собирается рядом с рабочим, сверяется сумма и только при совпадении
    выполняется mv. Не совпало — временный файл удаляется, рабочий остаётся
    нетронутым."""
    import hashlib
    data = open(local_path, 'rb').read()
    want = hashlib.sha256(data).hexdigest()
    staged = remote_path + '.new'

    remote_script = (
        'base64 -d > %(s)s; '
        'got=$(sha256sum %(s)s | cut -d" " -f1); '
        'if [ "$got" = "%(w)s" ]; then '
        '  mv %(s)s %(d)s && chown %(o)s %(d)s && chmod %(m)s %(d)s '
        '  && echo PUT_OK; '
        'else rm -f %(s)s; echo PUT_MISMATCH:$got; fi'
        % {'s': staged, 'd': remote_path, 'w': want,
           'o': owner, 'm': mode})

    fd, tmp = tempfile.mkstemp()
    try:
        os.write(fd, base64.b64encode(data))
        os.close(fd)
        # Управляющий скрипт уходит АРГУМЕНТОМ, а не трубой: труба здесь
        # занята полезной нагрузкой. Он короткий (пути и одна сумма), в
        # предел длины команды не упирается.
        cmd = ("ssh -o StrictHostKeyChecking=accept-new "
               "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 "
               "root@%s '%s' < %s" % (HOST, remote_script, tmp))
        child = pexpect.spawn('/bin/sh', ['-c', cmd], timeout=timeout,
                              encoding='utf-8', codec_errors='replace')
        if child.expect(['assword:', pexpect.EOF,
                         pexpect.TIMEOUT]) == 0:
            child.sendline(PASSWORD)
        child.expect(pexpect.EOF, timeout=timeout)
        out = child.before or ""
        child.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if 'PUT_OK' in out:
        return True, want
    if 'PUT_MISMATCH' in out:
        got = out.split('PUT_MISMATCH:', 1)[1].split()[0]
        return False, 'сумма не сошлась: ожидалась %s, на принтере %s' % (
            want[:16], got[:16])
    return False, 'передача не подтверждена: %s' % out.strip()[-200:]


def run_script(script, timeout=300):
    """Залить скрипт на принтер и выполнить его ТАМ. Вернуть (вывод, код).

    В отличие от run(), длина скрипта ничем не ограничена: он уезжает
    стандартным вводом, а не аргументом команды. Одна сессия на всё:
    приём, запуск, удаление, возврат настоящего кода завершения.

    Нужно там, где команд много или они длинные, — например, разовая
    диагностика или пакетная правка на принтере. Для одиночных коротких
    команд по-прежнему проще run()."""
    payload = base64.b64encode(script.encode()).decode()
    fd, tmp = tempfile.mkstemp()
    try:
        os.write(fd, payload.encode())
        os.close(fd)
        # Скрипт кладётся во временный файл, выполняется и удаляется, а код
        # завершения сохраняется до удаления и возвращается наружу — иначе
        # наружу уехал бы код команды rm, то есть всегда ноль.
        # Тот же принцип: обёртка — аргумент, сам скрипт — стандартный ввод.
        # Иначе обёртка прочитала бы вход сама и запустила пустой файл,
        # молча вернув ноль.
        remote = ('f=/tmp/psh_$$.sh; base64 -d > "$f"; sh "$f"; '
                  'rc=$?; rm -f "$f"; exit $rc')
        cmd = ("ssh -o StrictHostKeyChecking=accept-new "
               "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 "
               "root@%s '%s' < %s" % (HOST, remote, tmp))
        child = pexpect.spawn('/bin/sh', ['-c', cmd], timeout=timeout,
                              encoding='utf-8', codec_errors='replace')
        if child.expect(['assword:', pexpect.EOF, pexpect.TIMEOUT]) == 0:
            child.sendline(PASSWORD)
        child.expect(pexpect.EOF, timeout=timeout)
        out = child.before or ""
        child.close()
        return out, child.exitstatus
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


if __name__ == '__main__':
    out, code = run(sys.stdin.read())
    print(out)
    sys.exit(code if code is not None else 1)
