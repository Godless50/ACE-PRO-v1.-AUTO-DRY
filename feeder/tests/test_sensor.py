import sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ace_rh_feeder.sensor import Reading, decode


def test_decodes_real_payload():
    """Пакет, снятый с живого датчика: 26.95 C, 57 %RH, 3033 мВ."""
    r = decode(bytes.fromhex('870a39d90b'))
    assert r == Reading(rh=57.0, temp_c=26.95, battery_mv=3033)


def test_decodes_negative_temperature():
    r = decode(bytes.fromhex('50fb2ed90b'))
    assert r.temp_c == pytest.approx(-12.0, abs=0.01)


def test_rejects_short_payload():
    with pytest.raises(ValueError):
        decode(b'\x01\x02')


@pytest.mark.parametrize('rh', [101, 255])
def test_rejects_impossible_humidity(rh):
    payload = bytes.fromhex('870a') + bytes([rh]) + bytes.fromhex('d90b')
    with pytest.raises(ValueError):
        decode(payload)


def test_accepts_boundary_humidity_100():
    """100% — граничное, но допустимое значение влажности."""
    payload = bytes.fromhex('870a') + bytes([100]) + bytes.fromhex('d90b')
    r = decode(payload)
    assert r.rh == 100.0
