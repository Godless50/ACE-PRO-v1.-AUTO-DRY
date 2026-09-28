"""Отправка G-code в Moonraker. Только стандартная библиотека."""
import json
import logging
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

log = logging.getLogger(__name__)


class Moonraker:
    def __init__(self, base_url, timeout=15., opener=None):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def gcode(self, script):
        """Вернуть True при успехе. Недоступный принтер — не повод падать.

        Два разных отказа различаются в журнале: ответ с кодом ошибки
        (негодные параметры, Klipper в аварийном останове, команда не
        зарегистрирована) означает, что принтер доступен и команду ОТВЕРГ, —
        искать причину надо в команде и в Klipper, а не в сети.
        """
        url = '%s/printer/gcode/script' % self.base_url
        data = json.dumps({'script': script}).encode()
        request = urllib.request.Request(
            url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with self._open(request, timeout=self.timeout) as response:
                response.read()
            return True
        except urllib.error.HTTPError as e:
            log.warning('Moonraker отверг команду (%s): HTTP %s %s',
                        script, e.code, self._error_body(e))
            return False
        except Exception as e:
            log.warning('Moonraker недоступен (%s): %s', script, e)
            return False

    @staticmethod
    def _error_body(e):
        """Тело ответа с описанием отказа. Читается один раз и осторожно:
        пояснение Klipper ценно, но упасть на его чтении нельзя."""
        try:
            body = e.read()
        except Exception:
            return '(тело ответа недоступно)'
        if not body:
            return '(пустой ответ)'
        return body.decode('utf-8', 'replace')[:300]

    def set_rh(self, ace_index, rh, ttl, temp=None):
        """Отправить влажность на принтер."""
        rh_rounded = str(Decimal(str(rh)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))
        script = 'ACE_SET_RH ACE=%d RH=%s TTL=%d' % (ace_index, rh_rounded, ttl)
        if temp is not None:
            temp_rounded = str(Decimal(str(temp)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))
            script += ' TEMP=%s' % temp_rounded
        return self.gcode(script)
