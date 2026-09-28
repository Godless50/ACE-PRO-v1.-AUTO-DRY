import sys, os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ace_rh_feeder.main import Feeder, FAILURES_BEFORE_RESET
from ace_rh_feeder.sensor import Reading
from ace_rh_feeder.adapter import reset_adapter


class FakeSensor:
    def __init__(self, readings):
        self.readings = list(readings)

    async def read(self, timeout=30.):
        return self.readings.pop(0) if self.readings else None


class FakeMoonraker:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def set_rh(self, ace_index, rh, ttl, temp=None):
        self.calls.append((ace_index, rh, ttl, temp))
        return self.ok


class FakeResetter:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return True


def build(readings, ok=True):
    sensor = FakeSensor(readings)
    moon = FakeMoonraker(ok=ok)
    resetter = FakeResetter()
    return Feeder(sensor, moon, ace_index=0, ttl=1800,
                  resetter=resetter), moon, resetter


def test_sends_reading_to_printer():
    feeder, moon, _ = build([Reading(rh=57.0, temp_c=26.95, battery_mv=3033)])
    assert asyncio.run(feeder.run_once()) is True
    assert moon.calls == [(0, 57.0, 1800, 26.95)]


def test_does_not_send_stale_value_when_sensor_unavailable():
    """Главное требование: молчим, а не повторяем прошлое число."""
    feeder, moon, _ = build([Reading(rh=57.0, temp_c=26.9, battery_mv=3033),
                             None])
    asyncio.run(feeder.run_once())
    asyncio.run(feeder.run_once())
    assert len(moon.calls) == 1


def test_resets_adapter_after_repeated_failures():
    feeder, _, resetter = build([None] * (FAILURES_BEFORE_RESET + 1))
    for _ in range(FAILURES_BEFORE_RESET):
        asyncio.run(feeder.run_once())
    assert resetter.count == 1
    assert feeder.consecutive_failures == 0


def test_successful_read_clears_failure_counter():
    feeder, _, resetter = build([None,
                                 Reading(rh=50.0, temp_c=25., battery_mv=3000)])
    asyncio.run(feeder.run_once())
    asyncio.run(feeder.run_once())
    assert feeder.consecutive_failures == 0
    assert resetter.count == 0


def test_printer_unreachable_is_survivable():
    feeder, moon, _ = build([Reading(rh=57.0, temp_c=26.9, battery_mv=3033)],
                            ok=False)
    assert asyncio.run(feeder.run_once()) is False


def test_reset_adapter_issues_power_cycle():
    calls = []

    def runner(args):
        calls.append(args)
        return 0

    assert reset_adapter('hci0', runner=runner) is True
    assert calls == [['bluetoothctl', 'power', 'off'],
                     ['bluetoothctl', 'power', 'on']]


def test_reset_adapter_reports_failure_when_command_fails(caplog):
    """Код возврата bluetoothctl игнорировать нельзя: «перезапущен» в журнале
    при неудачной команде — ложь, из-за которой потерю датчика будут искать
    не там."""
    def runner(args):
        return 1 if args[-1] == 'on' else 0

    with caplog.at_level('WARNING'):
        assert reset_adapter('hci0', runner=runner) is False
    errors = [r for r in caplog.records if r.levelname == 'ERROR']
    assert errors and 'НЕ перезапущен' in errors[0].getMessage()


def test_reset_adapter_reports_success_when_commands_succeed(caplog):
    with caplog.at_level('WARNING'):
        assert reset_adapter('hci0', runner=lambda args: 0) is True
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'перезапущен' in text


def test_ttl_must_cover_several_polls():
    """Срок годности показания должен быть с запасом больше периода опроса:
    иначе показание протухает между опросами и нагрев мигает циклами длиной
    в период опроса."""
    from ace_rh_feeder.main import check_periods, TTL_TO_INTERVAL_RATIO
    check_periods(300, 1800)                      # текущая настройка
    check_periods(300, 300 * TTL_TO_INTERVAL_RATIO)   # ровно на границе
    try:
        check_periods(3600, 1800)
    except SystemExit as e:
        assert 'RH_TTL_SECONDS' in str(e)
        assert '3600' in str(e) and '1800' in str(e)
    else:
        raise AssertionError('несогласованные периоды должны прерывать запуск')
