import sys, os, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from ace_rh_feeder.moonraker import Moonraker


class FakeOpener:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode())
        self.calls.append((request.full_url, body, timeout))
        if self.fail:
            raise OSError('соединение отклонено')

        class R:
            def read(self_inner):
                return b'{"result": "ok"}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return R()


def test_sends_gcode_to_correct_endpoint():
    op = FakeOpener()
    m = Moonraker('http://printer:7125', opener=op)
    assert m.gcode('ACE_EXT_RH_STATUS') is True
    url, body, _ = op.calls[0]
    assert url == 'http://printer:7125/printer/gcode/script'
    assert body == {'script': 'ACE_EXT_RH_STATUS'}


def test_set_rh_formats_command():
    op = FakeOpener()
    m = Moonraker('http://printer:7125', opener=op)
    m.set_rh(0, 42.456, 1800, temp=26.95)
    assert op.calls[0][1]['script'] == \
        'ACE_SET_RH ACE=0 RH=42.5 TTL=1800 TEMP=27.0'


def test_set_rh_without_temperature():
    op = FakeOpener()
    m = Moonraker('http://printer:7125', opener=op)
    m.set_rh(0, 40.0, 600)
    assert op.calls[0][1]['script'] == 'ACE_SET_RH ACE=0 RH=40.0 TTL=600'


def test_returns_false_when_printer_unreachable():
    op = FakeOpener(fail=True)
    m = Moonraker('http://printer:7125', opener=op)
    assert m.gcode('ACE_EXT_RH_STATUS') is False


def http_error(code=400, body=b'{"error": {"message": "bad TTL"}}'):
    import io
    import urllib.error
    return urllib.error.HTTPError('http://printer:7125/printer/gcode/script',
                                  code, 'Bad Request', {}, io.BytesIO(body))


class FailingOpener:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, request, timeout=None):
        raise self.exc


def test_rejected_command_is_reported_as_rejection_not_unreachable(caplog):
    """Ответ с кодом ошибки (негодные параметры, Klipper в аварийном
    останове) — это НЕ недоступность принтера, и владелец должен видеть
    разницу в журнале."""
    m = Moonraker('http://printer:7125', opener=FailingOpener(http_error()))
    with caplog.at_level('WARNING'):
        assert m.gcode('ACE_SET_RH ACE=0 RH=999') is False
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'отверг' in text
    assert 'недоступен' not in text
    assert '400' in text
    assert 'bad TTL' in text


def test_connection_failure_is_reported_as_unreachable(caplog):
    m = Moonraker('http://printer:7125',
                  opener=FailingOpener(OSError('соединение отклонено')))
    with caplog.at_level('WARNING'):
        assert m.gcode('ACE_EXT_RH_STATUS') is False
    text = '\n'.join(r.getMessage() for r in caplog.records)
    assert 'недоступен' in text
    assert 'отверг' not in text
