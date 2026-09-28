"""Восстановление залипшего BLE-адаптера.

RTL8761BU перестаёт отдавать результаты LE-сканирования, оставаясь при этом
включённым и внешне исправным: классическое обнаружение продолжает работать,
в dmesg ничего нет. Лечится перезапуском питания контроллера через BlueZ.
"""
import logging
import subprocess
import time

log = logging.getLogger(__name__)


def reset_adapter(name=None, runner=None):
    """Перезапустить питание BLE-контроллера через bluetoothctl.

    Имя адаптера (hci0/hci1/...) на этой системе нестабильно: BlueZ может
    переназначить его после переинициализации USB, а сам контроллер при этом
    один и тот же. Поэтому `name` используется только для сообщения в
    журнал — команды `bluetoothctl power off/on` и так действуют на
    контроллер по умолчанию, указывать его явно не требуется.
    """
    run = runner or (lambda args: subprocess.run(args, timeout=20).returncode)
    label = name or 'контроллер по умолчанию'
    try:
        code_off = run(['bluetoothctl', 'power', 'off'])
        if runner is None:
            time.sleep(3)
        code_on = run(['bluetoothctl', 'power', 'on'])
        if runner is None:
            time.sleep(3)
        # Код возврата проверяется, а не игнорируется: запись «перезапущен»
        # при неудачной команде — ложь, из-за которой причину пропавшего
        # датчика будут искать не там.
        if code_off or code_on:
            log.error('адаптер %s НЕ перезапущен: power off вернул %s, '
                      'power on вернул %s', label, code_off, code_on)
            return False
        log.warning('адаптер %s перезапущен', label)
        return True
    except Exception as e:
        log.error('не удалось перезапустить адаптер %s: %s', label, e)
        return False
