"""Чтение Xiaomi LYWSD03MMC по BLE.

Сток-прошивка вещает показания зашифрованными (нужен bindkey из облака
Xiaomi), поэтому используется GATT-подключение: оно отдаёт значения
открытым текстом без облака и без ключа.
"""
import asyncio
import logging
import struct
from collections import namedtuple

from bleak import BleakClient, BleakScanner

CHAR_UUID = 'ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6'

Reading = namedtuple('Reading', 'rh temp_c battery_mv')

log = logging.getLogger(__name__)


def decode(payload):
    """5 байт: температура int16 LE /100, влажность uint8, батарея uint16 LE."""
    if len(payload) < 5:
        raise ValueError('пакет короче 5 байт: %r' % (payload,))
    raw_t, raw_h, mv = struct.unpack('<hBH', bytes(payload[:5]))
    if raw_h > 100:
        raise ValueError('влажность вне диапазона: %d' % raw_h)
    return Reading(rh=float(raw_h), temp_c=raw_t / 100., battery_mv=mv)


class XiaomiSensor:
    def __init__(self, mac, adapter=None):
        self.mac = mac
        self.adapter = adapter

    async def read(self, timeout=30.):
        """Вернуть показание или None. Исключения не пробрасываются:
        недоступный датчик — штатная ситуация, а не сбой сервиса."""
        try:
            # Номер адаптера (hci0/hci1/...) нестабилен: BlueZ может
            # переназначить его после переинициализации USB. Если адаптер
            # не задан явно, вообще не передаём его в bleak — пусть BlueZ
            # сам берёт свой единственный контроллер. 'adapter=' в bleak
            # 3.0.2 объявлен устаревшим, актуальный способ — bluez={...}.
            bluez_kwargs = {}
            if self.adapter is not None:
                bluez_kwargs['bluez'] = {'adapter': self.adapter}

            device = await BleakScanner.find_device_by_address(
                self.mac, timeout=timeout, **bluez_kwargs)
            if device is None:
                log.warning('датчик %s не найден в эфире', self.mac)
                return None
            result = {}
            done = asyncio.Event()

            def on_data(_, data):
                if 'r' not in result:
                    try:
                        result['r'] = decode(data)
                    except ValueError as e:
                        log.warning('негодный пакет: %s', e)
                        return
                    done.set()

            async with BleakClient(device, timeout=timeout, **bluez_kwargs) as client:
                await client.start_notify(CHAR_UUID, on_data)
                try:
                    await asyncio.wait_for(done.wait(), timeout=timeout)
                finally:
                    await client.stop_notify(CHAR_UUID)
            return result.get('r')
        except Exception as e:
            log.warning('чтение датчика не удалось: %s', e)
            return None
