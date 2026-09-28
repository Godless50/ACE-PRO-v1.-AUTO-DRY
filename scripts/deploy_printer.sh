#!/usr/bin/env bash
# Доставка модулей и конфигов на принтер с проверкой синтаксиса и
# контрольной суммы. Адрес принтера и пароль — только в scripts/psh.py,
# здесь их сознательно нет (единственный источник правды).
#
# Передача идёт функцией psh.put_file: один файл — одна ssh-сессия,
# полезная нагрузка уходит стандартным вводом. Раньше здесь была нарезка
# на куски по 4000 символов, потому что dropbear на этом принтере рвёт
# соединение на команде длиннее примерно 7000 символов. Ограничение
# касается только командной строки, у стандартного ввода его нет.
# Замер на живом принтере: модуль 24 КБ ехал девятью сессиями около пяти
# минут, теперь одной за шестнадцать секунд. Заодно исчез класс отказов,
# когда оборванная по таймауту попытка оставляла мусор в каталоге модулей.
set -euo pipefail
cd "$(dirname "$0")/.."

EXTRAS=${EXTRAS:-/home/lava/klipper/klippy/extras}
CFGDIR=${CFGDIR:-/home/lava/printer_data/config/extended/klipper}

for mod in printer/ace_ext_rh.py printer/ace_ext_raw.py; do
    python3 -c "import ast,sys; ast.parse(open('$mod').read())"
done
echo "синтаксис модулей в порядке"

python3 - "$EXTRAS" "$CFGDIR" <<'PY'
import sys
sys.path.insert(0, 'scripts')
from psh import put_file

extras, cfgdir = sys.argv[1], sys.argv[2]
pairs = [
    ('printer/ace_ext_rh.py',   '%s/ace_ext_rh.py' % extras),
    ('printer/ace_ext_rh.cfg',  '%s/ace_ext_rh.cfg' % cfgdir),
    ('printer/ace_ext_raw.py',  '%s/ace_ext_raw.py' % extras),
    ('printer/ace_ext_raw.cfg', '%s/ace_ext_raw.cfg' % cfgdir),
]
failed = False
for src, dst in pairs:
    ok, info = put_file(src, dst)
    if ok:
        print('%s: sha256 %s — совпало' % (dst, info[:16]))
    else:
        failed = True
        # Сверка и подмена делаются на принтере в одной сессии, поэтому
        # при несовпадении рабочий файл остаётся нетронутым.
        print('%s: НЕ ДОСТАВЛЕН (%s). Рабочий файл не изменён.' % (dst, info))
if failed:
    sys.exit(1)
PY

# Перезапуск прошивки перечитывает НОВЫЙ модуль extras, но не изменённый:
# процесс тот же, и модуль остаётся в sys.modules с прежним кодом.
# Проверено 2026-09-26 и ещё раз 2026-09-27.
# Мягкий путь: погасить процесс klippy и поднять его ветвью start. Ветвь
# restart того же скрипта делает лишний шаг — снимает питание с MCU
# (lava_io set MAIN_MCU_POWER=0 HEAD_MCU_POWER=0), а это обнуляет записи
# слотов ACE и привязки катушек.
echo "доставлено. Чтобы ИЗМЕНЁННЫЙ модуль заработал, klippy надо"
echo "перезапустить ПРОЦЕССОМ (перезапуск прошивки его не перечитывает):"
echo "  python3 scripts/psh.py <<'E'"
echo "  kill \$(ps -ef | grep klippy.py | grep -v grep | awk '{print \$2}' | head -1)"
echo "  sleep 4"
echo "  /etc/init.d/S60klipper start"
echo "  E"
