"""Курьер: читает датчик и передаёт влажность в multiACE.

Решений о нагреве не принимает — их принимает штатный механизм автосушки
multiACE на принтере.
"""
import asyncio
import logging
import os

from .adapter import reset_adapter
from .moonraker import Moonraker
from .sensor import XiaomiSensor

FAILURES_BEFORE_RESET = 3
# Во сколько раз срок годности показания (RH_TTL_SECONDS) обязан быть больше
# периода опроса (INTERVAL_SECONDS). Показание живёт на принтере ровно TTL,
# после чего модуль гасит нагрев принудительно, поэтому TTL должен покрывать
# несколько пропущенных опросов подряд: иначе показание протухает между
# опросами и нагрев мигает циклами длиной в период опроса. Тройка — это
# запас на два подряд неудачных чтения датчика (каждое может занять до
# нескольких таймаутов, см. сон в main) плюс само успешное.
TTL_TO_INTERVAL_RATIO = 3

log = logging.getLogger(__name__)


def check_periods(interval, ttl):
    """Прервать запуск, если период опроса и срок годности несогласованы."""
    if ttl < interval * TTL_TO_INTERVAL_RATIO:
        raise SystemExit(
            'несогласованные периоды: RH_TTL_SECONDS=%d при '
            'INTERVAL_SECONDS=%d. Срок годности показания должен быть не '
            'меньше %d-кратного периода опроса (нужно минимум %d), иначе '
            'показание протухает между опросами и нагрев сушилки мигает '
            'циклами длиной в период опроса.'
            % (ttl, interval, TTL_TO_INTERVAL_RATIO,
               interval * TTL_TO_INTERVAL_RATIO))


class Feeder:
    def __init__(self, sensor, moonraker, ace_index, ttl, resetter=None):
        self.sensor = sensor
        self.moonraker = moonraker
        self.ace_index = ace_index
        self.ttl = ttl
        self.resetter = resetter or reset_adapter
        self.consecutive_failures = 0

    async def run_once(self):
        """Одна итерация. При недоступном датчике НИЧЕГО не отправляется:
        старое значение продлило бы срок годности и оставило нагрев
        включённым вслепую."""
        reading = await self.sensor.read()
        if reading is None:
            self.consecutive_failures += 1
            log.warning('нет показаний подряд: %d', self.consecutive_failures)
            if self.consecutive_failures >= FAILURES_BEFORE_RESET:
                self.resetter()
                self.consecutive_failures = 0
            return False
        self.consecutive_failures = 0
        ok = self.moonraker.set_rh(self.ace_index, reading.rh, self.ttl,
                                   temp=reading.temp_c)
        log.info('%.1f%%rH, %.2f C, батарея %d мВ -> принтер: %s',
                 reading.rh, reading.temp_c, reading.battery_mv,
                 'принято' if ok else 'НЕ ДОСТАВЛЕНО')
        return ok


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    interval = int(os.environ.get('INTERVAL_SECONDS', '300'))
    # Пустое значение переменной означает «не указывать адаптер явно»:
    # номер контроллера (hci0/hci1/...) нестабилен на этой системе, и
    # XiaomiSensor(adapter=None) отдаёт выбор BlueZ.
    adapter_name = os.environ.get('BLE_ADAPTER') or None
    # Одно значение на оба применения: проверку соотношения и сам цикл.
    # Продублированное умолчание рано или поздно разъехалось бы, и проверка
    # стала бы проверять не то, с чем работает сервис.
    ttl = int(os.environ.get('RH_TTL_SECONDS', '1800'))
    check_periods(interval, ttl)
    feeder = Feeder(
        sensor=XiaomiSensor(os.environ['SENSOR_MAC'], adapter=adapter_name),
        moonraker=Moonraker(os.environ['MOONRAKER_URL']),
        ace_index=int(os.environ.get('ACE_INDEX', '0')),
        ttl=ttl,
        resetter=lambda: reset_adapter(adapter_name))
    log.info('запуск: опрос раз в %d с, срок годности показания %d с',
             interval, feeder.ttl)
    while True:
        await feeder.run_once()
        # Сон отсчитывается ПОСЛЕ завершения итерации, а не от её начала.
        # Само чтение (sensor.read) применяет свой таймаут последовательно
        # к поиску устройства, подключению и ожиданию уведомления, поэтому
        # в худшем случае одна итерация может занять около трёх номинальных
        # таймаутов чтения. Не считаем чтение мгновенным и не пытаемся
        # компенсировать этот дрейф — фактический период между отправками
        # будет «время чтения плюс interval», и это нормально при периоде
        # опроса в единицы минут.
        await asyncio.sleep(interval)


if __name__ == '__main__':
    asyncio.run(main())
