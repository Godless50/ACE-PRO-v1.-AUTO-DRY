import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'printer'))

from fakes import (AceWithout, FakeConfig, FakeGcode, FakeGcodeCommand,
                   FakeGcodeError,
                   FakePrinter)
import ace_ext_raw


class FakeProtocol:
    """Слой протокола в том объёме, который трогает замер длины кадра."""

    NAME = 'v1'

    def __init__(self):
        self.buffers = []

    def decode_frames(self, buffer):
        self.buffers.append(bytes(buffer))
        return []


class ProbeAce:
    """Объект multiACE в объёме, нужном модулю разведки."""

    def __init__(self, reply=None, raise_on_send=False):
        self.reactor = None  # выставляется в build()
        self._protocols = {0: FakeProtocol()}
        # Снимок состояния устройства: сюда multiACE складывает сырой ответ
        # get_status, отсюда модуль читает поля слотов.
        self._info_per_ace = {0: {'slots': [
            {'index': i, 'rfid': 0, 'sku': '', 'status': 'empty1'}
            for i in range(4)]}}
        self.sent = []
        self.reply = reply
        self.raise_on_send = raise_on_send
        # Привязки катушек, которые модуль попросил сделать.
        self.bound = []
        self._gate_status_per_ace = {0: [1, 1, 1, 1]}
        self._rc_last_uid = {}
        self.persisted = 0

    def _persist_tag_reads(self):
        self.persisted += 1

    def _spool_bind_by_tag(self, ace_idx, slot, sku, unbind=True):
        self.bound.append((ace_idx, slot, sku))
        return None

    def send_request_to(self, idx, request, callback):
        self.sent.append((idx, request))
        if self.raise_on_send:
            raise RuntimeError('устройство отключено')
        if self.reply is not None:
            # Диспетчер multiACE зовёт обратный вызов ИМЕНОВАННЫМИ
            # аргументами — воспроизводим именно этот стиль.
            callback(self=self, response=self.reply)


def build(ace=None, **cfg):
    ace = ace if ace is not None else ProbeAce()
    printer = FakePrinter(ace=ace)
    ace.reactor = printer.reactor
    module = ace_ext_raw.AceExtRaw(FakeConfig(printer, cfg))
    printer.fire('klippy:ready')
    return module, printer, ace


def run(printer, command, **params):
    cmd = FakeGcodeCommand({str(k): str(v) for k, v in params.items()})
    printer.lookup_object('gcode').commands[command](cmd)
    return cmd


def frame(payload_len):
    """Кадр устройства: 0xFF 0xAA, длина little-endian, затем нагрузка."""
    return b'\xff\xaa' + struct.pack('<H', payload_len) + b'x' * payload_len


# --- защита от случайного движения филамента --------------------------------

def test_unknown_method_refused_without_force():
    module, printer, ace = build()
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_RAW', METHOD='feed_filament')
    assert 'FORCE=1' in str(e.value)
    # Главное: до устройства команда не дошла.
    assert ace.sent == []


def test_unknown_method_allowed_with_force():
    module, printer, ace = build(ProbeAce(reply={'code': 0}))
    run(printer, 'ACE_EXT_RAW', METHOD='filament_identify', FORCE=1, INDEX=2)
    assert len(ace.sent) == 1
    idx, request = ace.sent[0]
    assert request['method'] == 'filament_identify'
    assert request['params'] == {'index': 2}


def test_safe_method_needs_no_force():
    module, printer, ace = build(ProbeAce(reply={'result': {'model': 'ACE'}}))
    cmd = run(printer, 'ACE_EXT_RAW', METHOD='get_info')
    assert ace.sent[0][1]['method'] == 'get_info'
    assert 'get_info' in cmd.responses[0]


# --- разбор параметров ------------------------------------------------------

def test_params_json_merged_with_index():
    module, printer, ace = build(ProbeAce(reply={'code': 0}))
    run(printer, 'ACE_EXT_RAW', METHOD='get_filament_info',
        PARAMS='{"a":1}', INDEX=3)
    assert ace.sent[0][1]['params'] == {'a': 1, 'index': 3}


def test_broken_params_rejected():
    module, printer, ace = build()
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_RAW', METHOD='get_status', PARAMS='{не json}')
    assert 'JSON' in str(e.value)
    assert ace.sent == []


def test_params_must_be_object():
    module, printer, ace = build()
    with pytest.raises(FakeGcodeError):
        run(printer, 'ACE_EXT_RAW', METHOD='get_status', PARAMS='[1,2]')


# --- поведение при молчании устройства --------------------------------------

def test_silence_reported_as_unknown_method():
    # Заводская прошивка на неизвестный метод просто не отвечает. Это и есть
    # главный признак, ради которого модуль написан.
    module, printer, ace = build(ProbeAce(reply=None))
    cmd = run(printer, 'ACE_EXT_RAW', METHOD='get_status')
    assert 'ответа нет' in cmd.responses[0]
    assert 'неизвестен' in cmd.responses[0]


def test_send_failure_does_not_crash():
    module, printer, ace = build(ProbeAce(raise_on_send=True))
    cmd = run(printer, 'ACE_EXT_RAW', METHOD='get_status')
    assert 'ответа нет' in cmd.responses[0]


# --- измерение длины кадра --------------------------------------------------

def test_scan_lengths_finds_longest_frame():
    state = {'max': 0, 'count': 0}
    buffer = frame(120) + frame(980) + frame(64)
    found = ace_ext_raw.AceExtRaw._scan_lengths(buffer, state)
    assert found == 3
    assert state['max'] == 980


def test_scan_lengths_ignores_absurd_lengths():
    # Случайное совпадение байтов посреди нагрузки не должно выдавать
    # фантастическую длину и портить измерение.
    state = {'max': 0, 'count': 0}
    buffer = b'\xff\xaa\xff\xff' + b'x' * 10
    ace_ext_raw.AceExtRaw._scan_lengths(buffer, state)
    assert state['max'] == 0


def test_frame_measurement_wraps_and_restores_protocol():
    module, printer, ace = build()
    protocol = ace._protocols[0]

    run(printer, 'ACE_EXT_FRAME_LEN', ACE=0, ARM=1)
    # Обёртка — обычная функция, у связанного метода есть __func__. Сравнивать
    # сами объекты нельзя: обращение к связанному методу каждый раз создаёт
    # новый объект, и проверка через is была бы ложноотрицательной.
    assert not hasattr(protocol.decode_frames, '__func__')

    # Прогоняем через обёртку настоящий кадр: разбор должен идти дальше по
    # цепочке, иначе замер сломал бы связь с устройством.
    protocol.decode_frames(frame(612))
    assert ace._protocols[0].buffers, 'исходный разбор не вызван'

    cmd = run(printer, 'ACE_EXT_FRAME_LEN', ACE=0, ARM=0)
    assert protocol.decode_frames.__func__ is FakeProtocol.decode_frames, (
        'обёртка не снята')
    assert '612' in cmd.responses[0]
    assert str(ace_ext_raw.FRAME_LIMIT - 612) in cmd.responses[0]


def test_double_arm_refused():
    module, printer, ace = build()
    run(printer, 'ACE_EXT_FRAME_LEN', ACE=0, ARM=1)
    with pytest.raises(FakeGcodeError):
        run(printer, 'ACE_EXT_FRAME_LEN', ACE=0, ARM=1)


def test_disarm_without_arm_refused():
    module, printer, ace = build()
    with pytest.raises(FakeGcodeError):
        run(printer, 'ACE_EXT_FRAME_LEN', ACE=0, ARM=0)


# --- совместимость ----------------------------------------------------------

def test_module_disables_itself_when_ace_is_missing():
    printer = FakePrinter(ace=None)
    module = ace_ext_raw.AceExtRaw(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    assert module.ace is None
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_RAW', METHOD='get_info')
    assert 'отключён' in str(e.value)


def test_module_disables_itself_when_attribute_is_gone():
    # Так выглядит обновление multiACE, переименовавшее приватный метод:
    # модуль обязан громко отключиться, а не молча делать вид, что работает.
    ace = AceWithout(ProbeAce(), 'send_request_to')
    printer = FakePrinter(ace=ace)
    module = ace_ext_raw.AceExtRaw(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    assert module.ace is None
    assert 'send_request_to' in module.disabled_reason


# --- тест антенн ------------------------------------------------------------

class DeviceAce(ProbeAce):
    """ACE, отвечающий на чтение метки и на запрос сведений о филаменте.

    Воспроизводит поведение живого устройства, включая его главную
    неприятность: при неудачном чтении сведения о филаменте отдают
    последнее удачное значение — чужое."""

    def __init__(self, per_slot=None, busy_slots=(), shared=''):
        super().__init__(reply=None)
        self.per_slot = dict(per_slot or {})
        self.busy_slots = set(busy_slots)
        self.shared = shared
        self.recognitions = []

    def send_request_to(self, idx, request, callback):
        self.sent.append((idx, request))
        method = request['method']
        slot = request.get('params', {}).get('index')
        if method == 'filament_recognition':
            self.recognitions.append(slot)
            if slot in self.busy_slots:
                callback(self=self, response={'code': 0, 'result': {},
                                              'msg': 'FORBIDDEN'})
                return
            if slot in self.per_slot:
                self.shared = self.per_slot[slot]
            callback(self=self, response={'code': 0, 'result': {},
                                          'msg': 'success'})
            return
        if method == 'get_filament_info':
            callback(self=self, response={'code': 0, 'result': {
                'index': slot, 'sku': self.shared, 'rfid': 1}})
            return
        callback(self=self, response={'code': 0, 'result': {}})


def build_device(ace):
    printer = FakePrinter(ace=ace)
    ace.reactor = printer.reactor
    ace_ext_raw.AceExtRaw(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    return printer


def test_antenna_test_reports_read_tag():
    ace = DeviceAce(per_slot={1: '04e55151c82a81'})
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, SLOT=1, ROUNDS=2)
    body = ' '.join(cmd.responses)
    assert ace.recognitions == [1, 1]
    assert 'слот 2' in body
    assert 'ответов 2 из 2' in body
    assert '04e55151c82a81' in body
    # Слоты сидят парами: 1-2 на первой плате, 3-4 на второй.
    assert 'NFC1' in body


def test_busy_slot_is_not_an_antenna_failure():
    # Слот с активной подачей устройство читать отказывается. Смешивать это
    # с «антенна не видит» нельзя: причина совсем другая.
    ace = DeviceAce(busy_slots={3})
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, SLOT=3, ROUNDS=2)
    body = ' '.join(cmd.responses)
    assert 'отказов «занято» 2' in body
    assert 'занят подачей' in body
    assert 'не отказ антенны' in body


def test_silence_names_both_possible_causes():
    ace = DeviceAce()
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, SLOT=0, ROUNDS=1)
    body = ' '.join(cmd.responses)
    assert 'ответов 0 из 1' in body
    assert 'метки нет вовсе' in body
    assert 'не напротив антенны' in body


def test_shared_buffer_trap_is_reported():
    """Главная защита модуля.

    На живом устройстве проверено: после удачного чтения одного слота
    запрос сведений по ЛЮБОМУ другому слоту возвращает тот же самый UID.
    Привязка катушки по такому значению списала бы расход не с той."""
    ace = DeviceAce(per_slot={0: '04e55151c82a81'})
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, ROUNDS=1)
    body = ' '.join(cmd.responses)
    assert 'ВНИМАНИЕ' in body
    assert '04e55151c82a81' in body
    assert 'НЕЛЬЗЯ' in body


def test_distinct_tags_do_not_trigger_the_warning():
    ace = DeviceAce(per_slot={0: 'aaaa1111', 1: 'bbbb2222',
                              2: 'cccc3333', 3: 'dddd4444'})
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, ROUNDS=1)
    body = ' '.join(cmd.responses)
    assert 'ВНИМАНИЕ' not in body
    assert ace.recognitions == [0, 1, 2, 3]


def test_antenna_test_refused_while_printing():
    class Printing:
        state = 'printing'

    ace = DeviceAce()
    printer = FakePrinter(ace=ace)
    ace.reactor = printer.reactor
    printer.objects['print_stats'] = Printing()
    ace_ext_raw.AceExtRaw(FakeConfig(printer, {}))
    printer.fire('klippy:ready')

    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, SLOT=0)
    assert 'простое' in str(e.value)
    assert ace.recognitions == []


# --- освобождение порта для прошивки ----------------------------------------

class FwAce(ProbeAce):
    """ACE с механизмом удержания порта, как в настоящем multiACE."""

    def __init__(self, printing=False, swapping=False, drying=()):
        super().__init__(reply={'code': 0})
        self._reconnecting_per_ace = {}
        self._spool_binding = {'0_0': '55', '0_1': '57'}
        self.guard_seen_open = None
        self._fw_update_hold = set()
        self._ace_devices = ['/dev/serial/by-path/fake-0']
        self._connected_per_ace = {0: True}
        self._swap_in_progress = swapping
        self._printing = printing
        self._auto_dry_started = set(drying)
        self.disconnected = []
        self.opened = []
        self.dry_stopped = []

    def _is_actively_printing(self):
        return self._printing

    def _disconnect_from(self, idx):
        self.disconnected.append(idx)
        self._connected_per_ace[idx] = False

    def _open_ace(self, idx, on_ready=None):
        # Настоящий multiACE отказывается открывать удерживаемый индекс —
        # воспроизводим это, иначе тест не поймает забытое снятие удержания.
        if idx in self._fw_update_hold:
            return False
        # Запоминаем состояние защитного флага НА МОМЕНТ открытия: если он
        # не выставлен, первый же пустой отчёт устройства снесёт привязки.
        self.guard_seen_open = self._reconnecting_per_ace.get(idx, False)
        self.opened.append(idx)
        self._connected_per_ace[idx] = True
        return True

    def _auto_dry_stop(self, idx, why):
        self.dry_stopped.append((idx, why))
        self._auto_dry_started.discard(idx)


def test_release_holds_port_and_disconnects():
    ace = FwAce()
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    assert 0 in ace._fw_update_hold
    assert ace.disconnected == [0]
    body = ' '.join(cmd.responses)
    assert '/dev/serial/by-path/fake-0' in body
    # Предупреждение обязано прозвучать: удержание живёт только в памяти.
    assert 'перезапускать НЕЛЬЗЯ' in body


def test_resume_clears_hold_and_reconnects():
    ace = FwAce()
    printer = build_device(ace)
    run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    cmd = run(printer, 'ACE_EXT_FW_RESUME', ACE=0)
    assert ace._fw_update_hold == set()
    assert ace.opened == [0]
    assert 'выполнено' in ' '.join(cmd.responses)


def test_release_stops_auto_dry():
    # Нагреватель, которым больше нечем управлять, оставлять включённым
    # нельзя: порт вот-вот закроется.
    ace = FwAce(drying=(0,))
    printer = build_device(ace)
    run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    assert ace.dry_stopped and ace.dry_stopped[0][0] == 0


def test_release_refused_while_printing():
    ace = FwAce(printing=True)
    printer = build_device(ace)
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    assert 'печать' in str(e.value)
    assert ace._fw_update_hold == set()
    assert ace.disconnected == []


def test_release_refused_during_swap():
    ace = FwAce(swapping=True)
    printer = build_device(ace)
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    assert 'смена филамента' in str(e.value)
    assert ace._fw_update_hold == set()


def test_release_refused_for_missing_device():
    ace = FwAce()
    printer = build_device(ace)
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_FW_RELEASE', ACE=2)
    assert 'не существует' in str(e.value)


def test_release_refused_on_incompatible_multiace():
    # Сборка multiACE без механизма удержания: отказ должен быть внятным,
    # а не падением по AttributeError.
    ace = ProbeAce()
    printer = build_device(ace)
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    assert '_fw_update_hold' in str(e.value)


def test_probe_commands_still_work_without_fw_support():
    # Отсутствие механизма удержания не должно отключать разведку.
    ace = ProbeAce(reply={'result': {'model': 'ACE'}})
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_RAW', METHOD='get_info')
    assert 'get_info' in cmd.responses[0]


def test_resume_guards_bindings_while_link_comes_up():
    """Главная защита возврата порта.

    multiACE освобождает привязку слота, как только видит его пустым, и
    гасит это флагом переподключения. Флаг ставится только в аварийном
    восстановлении, при ручном открытии порта — нет. После прошивки
    устройство перезагружается и первый отчёт может прийти с пустыми
    слотами, что стоило бы всех привязок."""
    ace = FwAce()
    printer = build_device(ace)
    run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    run(printer, 'ACE_EXT_FW_RESUME', ACE=0)
    assert ace.guard_seen_open is True, 'порт открыт без защиты привязок'
    # Флаг обязан сняться, иначе multiACE навсегда перестанет освобождать
    # привязки при реальном извлечении катушки.
    assert ace._reconnecting_per_ace.get(0) is False


def test_resume_reports_binding_count_and_losses():
    ace = FwAce()
    printer = build_device(ace)
    run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    cmd = run(printer, 'ACE_EXT_FW_RESUME', ACE=0)
    body = ' '.join(cmd.responses)
    assert 'привязок было 2, стало 2' in body
    assert 'ПОТЕРЯНЫ' not in body


def test_resume_names_lost_bindings():
    ace = FwAce()
    printer = build_device(ace)
    run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    # Имитируем то, от чего защищаемся: связь поднялась и привязка пропала.
    orig = ace._open_ace
    def losing_open(idx, on_ready=None):
        r = orig(idx, on_ready)
        ace._spool_binding.pop('0_1', None)
        return r
    ace._open_ace = losing_open
    cmd = run(printer, 'ACE_EXT_FW_RESUME', ACE=0)
    body = ' '.join(cmd.responses)
    assert 'ПОТЕРЯНЫ: 0_1' in body


def test_resume_clears_guard_even_if_open_raises():
    ace = FwAce()
    printer = build_device(ace)
    run(printer, 'ACE_EXT_FW_RELEASE', ACE=0)
    def boom(idx, on_ready=None):
        raise RuntimeError('порт занят')
    ace._open_ace = boom
    cmd = run(printer, 'ACE_EXT_FW_RESUME', ACE=0)
    # Оставить флаг висеть нельзя: multiACE перестал бы освобождать
    # привязки при реальном извлечении катушки, и это было бы молчаливо.
    assert ace._reconnecting_per_ace.get(0) is False
    assert 'НЕ УДАЛОСЬ' in ' '.join(cmd.responses)


def test_busy_slot_retried_not_counted_as_silence():
    """Отказ «занято» не должен сжигать раунд.

    Устройство принимает чтение, а следующие секунды отвечает отказом.
    Замер на живом ACE: без выдержки двадцать раундов укладывались в три
    секунды и давали ноль ответов там, где с выдержкой чтение проходит."""
    ace = DeviceAce(per_slot={0: 'aabbccdd'})
    # Первые два обращения — «занято», третье принимается.
    seq = ['FORBIDDEN', 'FORBIDDEN', 'success']
    calls = []

    orig = ace.send_request_to
    def flaky(idx, request, callback):
        if request['method'] == 'filament_recognition':
            calls.append(1)
            msg = seq[min(len(calls) - 1, len(seq) - 1)]
            if msg == 'success':
                ace.shared = ace.per_slot.get(request['params']['index'], '')
            callback(self=ace, response={'code': 0, 'result': {}, 'msg': msg})
            return
        orig(idx, request, callback)
    ace.send_request_to = flaky

    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, SLOT=0, ROUNDS=1)
    body = ' '.join(cmd.responses)
    assert len(calls) == 3, 'повторов после отказа не было'
    assert 'ответов 1 из 1' in body
    assert 'aabbccdd' in body


def test_persistent_busy_is_reported_as_busy_not_as_no_answer():
    ace = DeviceAce(busy_slots={2})
    printer = build_device(ace)
    cmd = run(printer, 'ACE_EXT_ANTENNA_TEST', ACE=0, SLOT=2, ROUNDS=1)
    body = ' '.join(cmd.responses)
    assert 'отказов «занято» 1' in body
    assert 'занят подачей' in body
    # Каждый раунд должен был честно попробовать несколько раз.
    assert len(ace.recognitions) == ace_ext_raw.BUSY_RETRIES + 1


# --- разбор PARAMS без кавычек ----------------------------------------------

def test_params_plain_form_parsed():
    """Форма без кавычек нужна потому, что до модуля кавычки доезжают не
    всегда: строка проходит разбор G-code, и JSON приходил уже без них."""
    module, printer, ace = build(ProbeAce(reply={'code': 0}))
    run(printer, 'ACE_EXT_RAW', METHOD='read_test_result', PARAMS='type=1', FORCE=1)
    assert ace.sent[0][1]['params'] == {'type': 1}


def test_params_plain_form_multiple_and_types():
    module, printer, ace = build(ProbeAce(reply={'code': 0}))
    run(printer, 'ACE_EXT_RAW', METHOD='x', PARAMS='type=2,name=abc,hex=0x10',
        FORCE=1)
    assert ace.sent[0][1]['params'] == {'type': 2, 'name': 'abc', 'hex': 16}


def test_params_plain_form_merges_with_index():
    module, printer, ace = build(ProbeAce(reply={'code': 0}))
    run(printer, 'ACE_EXT_RAW', METHOD='x', PARAMS='type=3', INDEX=2, FORCE=1)
    assert ace.sent[0][1]['params'] == {'type': 3, 'index': 2}


def test_params_plain_form_rejects_garbage():
    module, printer, ace = build()
    with pytest.raises(FakeGcodeError) as e:
        run(printer, 'ACE_EXT_RAW', METHOD='x', PARAMS='typeisone', FORCE=1)
    assert 'имя=значение' in str(e.value)
    assert ace.sent == []


def test_params_json_form_still_works():
    module, printer, ace = build(ProbeAce(reply={'code': 0}))
    run(printer, 'ACE_EXT_RAW', METHOD='x', PARAMS='{"a":1}', FORCE=1)
    assert ace.sent[0][1]['params'] == {'a': 1}


# --- туннель к считывателю: чтение UID -------------------------------------

# Сырой буфер антиколлизии для метки HF Yellow, UID которой подтверждён
# телефоном (NFC Tools: 04:EF:51:51:C8:2A:81, ATQA 0x0044, SAK 0x00).
# Раскладка ISO14443A для двойного UID: CT 0x88, три байта первого уровня,
# BCC, четыре байта второго уровня, BCC.
HF_YELLOW_BUF = bytes.fromhex('88 04 EF 51 32 51 C8 2A 81 32 00 00')
HF_YELLOW_UID = '04EF5151C82A81'


class TunnelAce(ProbeAce):
    """ACE с прошивкой-туннелем: значение INDEX >= 0x20000 читает/пишет
    регистры считывателя, 0x3D отдаёт байт статуса, 0x3E — четыре байта
    сырого буфера по смещению. Ответ туннеля: {"id":N,"v":N}."""

    def __init__(self, buf=HF_YELLOW_BUF, status=1, version=0xA1):
        super().__init__()
        self.buf = bytes(buf) + b'\0' * 16
        self.status = status
        self.version = version
        self.identified = []
        self.plain = []

    def send_request_to(self, idx, request, callback):
        self.sent.append((idx, request))
        packed = int(request['params'].get('index', 0))
        if packed < 0x20000:
            self.plain.append(packed)
            callback(self=self, response={'code': 0, 'msg': 'success'})
            return
        reader = (packed >> 15) & 3
        flag = (packed >> 14) & 1
        reg = (packed >> 8) & 0x3F
        data = packed & 0xFF
        if reg == 0x3E:
            if flag:
                self.identified.append(reader)
            v = int.from_bytes(self.buf[data:data + 4], 'big')
        elif reg == 0x3D:
            v = self.status
        elif reg == 0x37:
            v = self.version
        else:
            v = 0
        callback(self=self, response={'id': 7, 'v': v})


def test_pack_versionreg_reader0_matches_agreed_value():
    # Значение, согласованное с DeepSeek для первой проверки туннеля.
    assert ace_ext_raw.pack_tunnel(0, 0, 0x37) == 145152 == 0x23700


def test_pack_fields_land_in_agreed_bits():
    p = ace_ext_raw.pack_tunnel(reader=1, write=1, reg=0x14, data=0x83)
    assert p >> 17 == 1
    assert (p >> 15) & 3 == 1
    assert (p >> 14) & 1 == 1
    assert (p >> 8) & 0x3F == 0x14
    assert p & 0xFF == 0x83
    assert ace_ext_raw.pack_tunnel(3, 0, 0) == 0x38000


@pytest.mark.parametrize('bad', [
    dict(reader=4, write=0, reg=0), dict(reader=-1, write=0, reg=0),
    dict(reader=0, write=0, reg=0x40), dict(reader=0, write=0, reg=0, data=256),
    dict(reader=0, write=2, reg=0)])
def test_pack_rejects_out_of_range(bad):
    # Таблица распиновки в прошивке индексируется без проверки границ —
    # граница обязана стоять здесь.
    with pytest.raises(ValueError):
        ace_ext_raw.pack_tunnel(**bad)


def test_tunnel_value_from_both_reply_shapes():
    assert ace_ext_raw.tunnel_value({'id': 1, 'v': 161}) == 161
    assert ace_ext_raw.tunnel_value({'result': {'v': 161}}) == 161
    assert ace_ext_raw.tunnel_value({'code': 0}) is None
    assert ace_ext_raw.tunnel_value(None) is None


def test_word_to_bytes_is_big_endian_like_the_stub():
    assert ace_ext_raw.word_bytes(0x8804EF51) == b'\x88\x04\xef\x51'


def test_uid_from_phone_verified_buffer():
    assert ace_ext_raw.uid_from_buffer(HF_YELLOW_BUF) == HF_YELLOW_UID


@pytest.mark.parametrize('buf', [
    bytes(12),                                   # пусто
    bytes.fromhex('88 04 E5 51 32 51 C8 2A 81 32 00 00'),  # битый байт, BCC0 не сходится
    bytes.fromhex('88 04 EF 51 32 51 C8 2A 81 33 00 00'),  # BCC1 не сходится
    bytes.fromhex('04 EF 51 51 32 51 C8 2A 81 32 00 00'),  # нет каскадного тега
    HF_YELLOW_BUF[:9],                           # обрезан
])
def test_uid_rejected_when_buffer_inconsistent(buf):
    assert ace_ext_raw.uid_from_buffer(buf) is None


def test_uid_read_command_reads_identify_then_buffer():
    ace = TunnelAce()
    module, printer, ace = build(ace)
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    text = '\n'.join(cmd.responses)
    assert HF_YELLOW_UID in text
    # Заходов ровно два: столько нужно, чтобы два чтения совпали.
    assert ace.identified == [0, 0]
    # Порядок внутри захода: опознание с нулевым смещением, затем два
    # чтения хвоста, затем статус. Ни одного обычного filament_recognition.
    regs = [((r['params']['index'] >> 8) & 0x3F, r['params']['index'] & 0xFF,
             (r['params']['index'] >> 14) & 1) for _, r in ace.sent]
    one_pass = [(0x3E, 0, 1), (0x3E, 4, 0), (0x3E, 8, 0), (0x3D, 0, 0)]
    assert regs == one_pass * 2
    assert ace.plain == []
    assert all(r['method'] == 'filament_recognition' for _, r in ace.sent)


def test_uid_read_reports_no_tag_on_empty_buffer():
    module, printer, ace = build(TunnelAce(buf=bytes(12), status=0))
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=1)
    text = '\n'.join(cmd.responses)
    assert 'нет метки' in text
    # Пустой слот опрашивается до конца всех раундов, а не один раз.
    assert ace.identified == [1] * ace_ext_raw.UID_ROUNDS


def test_uid_read_refuses_reader_out_of_range():
    module, printer, ace = build(TunnelAce())
    with pytest.raises(FakeGcodeError):
        run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=4)
    assert ace.sent == []


def test_uid_read_reports_silence_when_tunnel_absent():
    # Заводская прошивка отвечает на filament_recognition обычным ответом
    # без поля v — это должно читаться как «туннеля нет», а не как UID.
    module, printer, ace = build(ProbeAce(reply={'code': 0, 'msg': 'success'}))
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    assert 'туннел' in '\n'.join(cmd.responses).lower()


def test_uid_read_distinguishes_silence_from_missing_tunnel():
    # Устройство не ответило вовсе. Это НЕ «заводская прошивка без
    # туннеля»: смешивать молчание с отсутствием возможности нельзя —
    # ровно этой подменой смыслов бесполезен штатный флаг rfid.
    module, printer, ace = build(ProbeAce(reply=None))
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    text = '\n'.join(cmd.responses)
    assert 'не ответил' in text
    # Сообщение про молчание само упоминает туннель, чтобы объяснить
    # разницу, поэтому отличаем ветки по слову «заводская» — оно стоит
    # только в ответе «туннеля в прошивке нет».
    assert 'заводская' not in text


def test_uid_read_reports_the_reader_number_the_operator_typed():
    # Команда принимает считыватели 0..3, значит и в ответе должен стоять
    # тот же номер. Сдвиг на единицу в сообщении опасен именно сейчас:
    # следующая задача — карта «считыватель → слот», и сбитая нумерация
    # привяжет катушку не к тому слоту.
    module, printer, ace = build(TunnelAce())
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=2)
    assert 'считывателя 2' in '\n'.join(cmd.responses)


# --- устойчивость чтения: два совпавших подряд -------------------------------

class FlakyTunnelAce(TunnelAce):
    """Считыватель, который отвечает не с каждой попытки. Так ведёт себя
    железо, когда в поле одной антенны оказываются две метки: штатная
    антиколлизия возвращает то одну, то ничего."""

    def __init__(self, pattern):
        super().__init__()
        # pattern — список буферов, по одному на каждое опознание.
        self.pattern = list(pattern)
        self.rounds = 0

    def send_request_to(self, idx, request, callback):
        packed = int(request['params'].get('index', 0))
        if packed >= 0x20000 and ((packed >> 14) & 1) and \
                ((packed >> 8) & 0x3F) == 0x3E:
            i = min(self.rounds, len(self.pattern) - 1)
            self.buf = bytes(self.pattern[i]) + b'\0' * 16
            self.rounds += 1
        super().send_request_to(idx, request, callback)


OTHER_BUF = bytes.fromhex('88 04 12 51 CF 51 C8 2A 81 32 00 00')
OTHER_UID = '04125151C82A81'


def test_stable_read_accepts_two_matching_rounds():
    ace = FlakyTunnelAce([HF_YELLOW_BUF, HF_YELLOW_BUF])
    module, printer, ace = build(ace)
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    assert HF_YELLOW_UID in '\n'.join(cmd.responses)
    assert ace.rounds == 2


def test_stable_read_retries_over_a_single_miss():
    # Первая попытка пустая, дальше метка читается дважды одинаково.
    ace = FlakyTunnelAce([bytes(12), HF_YELLOW_BUF, HF_YELLOW_BUF])
    module, printer, ace = build(ace)
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    assert HF_YELLOW_UID in '\n'.join(cmd.responses)


def test_stable_read_refuses_two_different_tags():
    # Две метки в поле одной антенны: каждый раунд отдаёт другую. Такое
    # чтение принимать нельзя — привязали бы катушку не к тому слоту.
    ace = FlakyTunnelAce([HF_YELLOW_BUF, OTHER_BUF] * 4)
    module, printer, ace = build(ace)
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    text = '\n'.join(cmd.responses)
    assert 'неустойчив' in text
    assert HF_YELLOW_UID in text and OTHER_UID in text


def test_stable_read_reports_no_tag_when_all_rounds_empty():
    ace = FlakyTunnelAce([bytes(12)] * 8)
    module, printer, ace = build(ace)
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    assert 'нет метки' in '\n'.join(cmd.responses)


def test_word_bytes_survives_the_signed_form_the_firmware_prints():
    # Шаблон ответа в прошивке печатает значение через %d со знаком,
    # поэтому слово со старшим установленным битом приходит
    # отрицательным. В живом журнале это выглядело как v=-2007809424.
    # Разбор обязан дать те же байты, что и для беззнаковой записи.
    assert ace_ext_raw.word_bytes(-2007809424) == bytes.fromhex('88534270')
    assert ace_ext_raw.word_bytes(-2007809424) == \
        ace_ext_raw.word_bytes(0x88534270)
    assert ace_ext_raw.word_bytes(-372132608) == bytes.fromhex('e9d1b500')


def test_uid_read_assembles_a_tag_whose_words_arrive_signed():
    # Заводская метка Anycubic, кадр снят с живого устройства: слова
    # пришли как -2007809424, -372132608, 23396352. UID совпадает с
    # записанным в Spoolman у катушки 20.
    buf = bytes.fromhex('88 53 42 70 E9 D1 B5 00 01 65 00 00')
    ace = FlakyTunnelAce([buf, buf])
    module, printer, ace = build(ace)
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=1)
    assert '534270D1B50001' in '\n'.join(cmd.responses)


# --- оборванное чтение метки ------------------------------------------------

def test_uid_rejects_zeroed_second_cascade_level():
    # Снято с живого устройства 2026-09-28: считыватель отдал
    # 53427000000000 вместо 534270D1B50001. Второй уровень каскада
    # пришёл нулями, и контрольный байт для нулей тоже ноль, поэтому
    # кадр формально сходится. Это оборванное чтение на краю поля, а не
    # метка: принять его значит привязать катушку к выдуманному UID.
    buf = bytes.fromhex('88 53 42 70 E9 00 00 00 00 00 00 00')
    assert ace_ext_raw.uid_from_buffer(buf) is None


def test_uid_rejects_zeroed_first_cascade_level():
    # Зеркальный случай: нули в первом уровне при сходящемся контроле.
    buf = bytes.fromhex('88 00 00 00 88 51 C8 2A 81 32 00 00')
    assert ace_ext_raw.uid_from_buffer(buf) is None


def test_uid_still_accepts_a_tag_with_a_single_zero_byte():
    # Ноль внутри уровня — не повод отказывать: нулевые байты в UID
    # встречаются. Отвергаем только полностью нулевой уровень.
    buf = bytearray(HF_YELLOW_BUF)
    buf[6] = 0x00
    buf[9] = buf[5] ^ buf[6] ^ buf[7] ^ buf[8]
    assert ace_ext_raw.uid_from_buffer(bytes(buf)) == '04EF515100 2A81'.replace(' ', '')


def test_intermittent_single_tag_is_not_called_a_collision():
    # Метка одна, но ловится через раз (край поля). Это НЕ «две метки в
    # поле»: путать их нельзя, диагноз и лечение разные — здесь надо
    # довернуть катушку, а там убрать соседа.
    ace = FlakyTunnelAce([HF_YELLOW_BUF, bytes(12)] * 4)
    module, printer, ace = build(ace)
    cmd = run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0)
    text = '\n'.join(cmd.responses)
    assert 'через раз' in text
    assert HF_YELLOW_UID in text
    assert 'две метки' not in text


def test_two_tags_in_the_field_are_still_called_a_collision():
    ace = FlakyTunnelAce([HF_YELLOW_BUF, OTHER_BUF] * 4)
    module, printer, ace = build(ace)
    text = '\n'.join(run(printer, 'ACE_EXT_UID_READ', ACE=0, READER=0).responses)
    assert 'две метки' in text
    assert HF_YELLOW_UID in text and OTHER_UID in text


# --- поиск метки доворотом катушки ------------------------------------------

class SeekAce(TunnelAce):
    """Устройство, где метка слота видна только в своём секторе поворота.

    Модель проста и отражает найденное на железе: у каждой катушки пары
    есть диапазон «намотанной длины», в котором её метка стоит напротив
    антенны. Пока в секторе стоит соседняя катушка, она забивает эфир.
    """

    def __init__(self, sectors, occupied=(True, True, True, True)):
        super().__init__()
        # sectors: слот -> (буфер метки, начало сектора, конец сектора)
        self.sectors = sectors
        self.moved = {0: 0, 1: 0, 2: 0, 3: 0}
        self.occupied = list(occupied)
        self._gate_status_per_ace = {0: [1 if o else 0 for o in self.occupied]}

    def _visible(self, reader):
        out = []
        for slot in (reader * 2, reader * 2 + 1):
            spec = self.sectors.get(slot)
            if spec is None or not self.occupied[slot]:
                continue
            buf, lo, hi = spec
            if lo <= self.moved[slot] <= hi:
                out.append(buf)
        return out

    def send_request_to(self, idx, request, callback):
        packed = int(request['params'].get('index', 0))
        if packed >= 0x20000 and ((packed >> 14) & 1) and \
                ((packed >> 8) & 0x3F) == 0x3E:
            reader = (packed >> 15) & 3
            seen = self._visible(reader)
            if len(seen) == 1:
                self.buf = seen[0] + b'\0' * 16
            elif len(seen) > 1:
                # Две метки в поле: устройство отдаёт их по очереди.
                self.rounds = getattr(self, 'rounds', 0) + 1
                self.buf = seen[self.rounds % len(seen)] + b'\0' * 16
            else:
                self.buf = bytes(28)
        super().send_request_to(idx, request, callback)

    def retract(self, slot, length):
        self.moved[slot] += length
        # Модель износа: после lose_after миллиметров смотки пруток
        # выходит из подающих шестерён и гейт слота гаснет.
        limit = getattr(self, 'lose_after', None)
        if limit is not None:
            gates = self._gate_status_per_ace[0]
            gates[slot] = 0 if self.moved[slot] >= limit else 1


class SeekGcode(FakeGcode):
    """Перехватывает ACE_RETRACT и двигает модель."""

    def __init__(self, ace):
        super().__init__()
        self.ace = ace

    def run_script_from_command(self, script):
        self.scripts.append(script)
        if script.startswith(('ACE_RETRACT', 'ACE_FEED')):
            parts = dict(p.split('=') for p in script.split()[1:])
            length = int(parts['LENGTH'])
            if script.startswith('ACE_FEED'):
                length = -length
            self.ace.retract(int(parts['INDEX']), length)


def build_seek(ace):
    printer = FakePrinter(ace=ace)
    ace.reactor = printer.reactor
    gcode = SeekGcode(ace)
    printer.objects['gcode'] = gcode
    module = ace_ext_raw.AceExtRaw(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    return module, printer, ace


def retracted(printer, slot):
    """Сколько смотали с катушки за прогон. Смотрим на выданные команды, а
    не на конечное положение: катушки возвращаются на место, и по остатку
    пройденного пути не видно."""
    return sum(int(x.split('LENGTH=')[1].split()[0])
               for x in printer.lookup_object('gcode').scripts
               if x.startswith('ACE_RETRACT INDEX=%d ' % slot))


def test_seek_finds_tag_already_in_sector_without_moving():
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 100)}, occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    # VERIFY=0: здесь проверяется только сам поиск. Подтверждение
    # принадлежности движением обязательно двигает катушку и разобрано
    # отдельными тестами ниже.
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=0)
    text = '\n'.join(cmd.responses)
    assert HF_YELLOW_UID in text
    assert printer.lookup_object('gcode').scripts == []


def test_seek_rotates_own_spool_until_the_tag_enters_the_sector():
    # Метка своей катушки войдёт в сектор после 16 мм подачи.
    ace = SeekAce({0: (HF_YELLOW_BUF, 16, 100)}, occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, STEP=8, RESTORE=0)
    text = '\n'.join(cmd.responses)
    assert HF_YELLOW_UID in text
    assert ace.moved[0] >= 16
    assert all(s.startswith('ACE_RETRACT INDEX=0') for s in
               printer.lookup_object('gcode').scripts)


def test_seek_nudges_the_neighbour_out_of_the_field():
    # Обе метки пары в секторе: сосед мешает. Уводим соседа и читаем свою.
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 100), 1: (OTHER_BUF, 0, 20)})
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, NUDGE=25)
    text = '\n'.join(cmd.responses)
    assert HF_YELLOW_UID in text
    assert OTHER_UID not in text
    # Двигали именно соседа, а не свою катушку.
    assert retracted(printer, 1) > 0


def test_seek_never_moves_an_empty_neighbour():
    ace = SeekAce({0: (HF_YELLOW_BUF, 24, 100)},
                  occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, STEP=8)
    assert ace.moved[1] == 0


def test_seek_gives_up_and_reports_instead_of_moving_forever():
    ace = SeekAce({}, occupied=(True, True, False, False))
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, STEP=8, ROUNDS=3)
    text = '\n'.join(cmd.responses)
    assert 'не найдена' in text
    assert ace.moved[0] <= 3 * 8


def test_seek_binds_the_spool_when_the_tag_is_found():
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 100)}, occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=0)
    assert ace.bound == [(0, 0, HF_YELLOW_UID)]


def test_seek_refuses_during_printing():
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 100)})
    module, printer, ace = build_seek(ace)
    printer.objects['print_stats'] = type('S', (), {'state': 'printing'})()
    with pytest.raises(FakeGcodeError):
        run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0)
    assert printer.lookup_object('gcode').scripts == []


# --- принадлежность метки подтверждается движением --------------------------

def test_seek_confirms_ownership_by_moving_our_own_spool():
    # Сосед занят, но его метка вне сектора, а своя уходит из сектора
    # после доворота — значит найденная метка наша.
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 20), 1: (OTHER_BUF, 500, 600)})
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=1)
    text = '\n'.join(cmd.responses)
    assert HF_YELLOW_UID in text
    assert 'подтверждена движением' in text
    assert ace.bound == [(0, 0, HF_YELLOW_UID)]


def test_seek_refuses_to_bind_a_tag_that_belongs_to_the_neighbour():
    # В поле только метка СОСЕДА (слот 1), своя катушка пуста по метке.
    # Проворот своей катушки её не трогает, проворот соседа — убирает.
    # Привязывать такую метку к нашему слоту нельзя: это молча неверный
    # учёт расхода, худшая из возможных ошибок здесь.
    ace = SeekAce({1: (OTHER_BUF, 0, 20)})
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=1)
    text = '\n'.join(cmd.responses)
    assert 'соседн' in text
    assert ace.bound == []


def test_seek_reports_honestly_when_ownership_stays_unclear():
    # Метка видна всегда и не уходит ни от своего проворота, ни от
    # соседского: решить нельзя, и врать про это нельзя.
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 10000)})
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=1)
    text = '\n'.join(cmd.responses)
    assert 'не удалось подтвердить' in text
    assert ace.bound == []


def test_verify_off_keeps_the_old_behaviour():
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 10000)})
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=0)
    assert ace.bound == [(0, 0, HF_YELLOW_UID)]


def test_ownership_is_certain_when_the_neighbour_slot_is_empty():
    # Соседний слот пуст — чужой метке взяться неоткуда. Это надо решать
    # без единого движения: катушку зря не крутим.
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 10000)},
                  occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=1)
    text = '\n'.join(cmd.responses)
    assert 'сосед' in text and 'пуст' in text
    assert ace.bound == [(0, 0, HF_YELLOW_UID)]
    assert printer.lookup_object('gcode').scripts == []


def test_verification_keeps_turning_until_the_tag_leaves_the_sector():
    # Сектор шире одного доворота: метка уходит только на 60 мм. Один шаг
    # в 25 мм её не выведет, и на этом нельзя останавливаться — иначе
    # своя же метка будет объявлена неопознанной.
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 60), 1: (OTHER_BUF, 500, 600)})
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=1)
    text = '\n'.join(cmd.responses)
    assert 'подтверждена движением' in text
    assert retracted(printer, 0) > 25
    assert ace.bound == [(0, 0, HF_YELLOW_UID)]


def test_verification_respects_the_movement_budget():
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 10000)})
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, VERIFY=1, VERIFY_MAX=60)
    # Бюджет на каждую катушку соблюдён: бесконечно крутить нельзя.
    assert ace.moved[0] <= 60 and ace.moved[1] <= 60


# --- возврат катушек в исходное положение -----------------------------------

def test_seek_returns_every_spool_it_turned_to_where_it_was():
    # Найдено на живом: подтверждение провернуло соседнюю катушку на
    # 100 мм и оставило её там. Следующий поиск по соседнему слоту начал
    # с испорченной позиции и метку уже не нашёл. Каждая команда обязана
    # оставлять устройство в том положении, в котором его взяла.
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 20), 1: (OTHER_BUF, 500, 600)})
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0)
    assert ace.moved == {0: 0, 1: 0, 2: 0, 3: 0}


def test_restore_feeds_back_exactly_what_was_retracted():
    ace = SeekAce({0: (HF_YELLOW_BUF, 40, 100)},
                  occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, STEP=8)
    scripts = printer.lookup_object('gcode').scripts
    back = sum(int(s.split('LENGTH=')[1].split()[0])
               for s in scripts if s.startswith('ACE_FEED'))
    out = sum(int(s.split('LENGTH=')[1].split()[0])
              for s in scripts if s.startswith('ACE_RETRACT'))
    assert back == out and out > 0


def test_restore_can_be_switched_off():
    ace = SeekAce({0: (HF_YELLOW_BUF, 40, 100)},
                  occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0, STEP=8, RESTORE=0)
    assert ace.moved[0] > 0


# --- масштаб доворота и защита прутка ---------------------------------------

def test_seek_sweeps_a_whole_revolution_not_a_few_degrees():
    # Владелец: «ты вытаскиваешь пруток по 2 см, а надо протягивать 40 см».
    # Метка сидит на катушке, и чтобы провести её мимо антенны, нужен
    # оборот, а не несколько градусов. Сектор здесь открывается на 300 мм —
    # прежний шаг в 8 мм не дошёл бы туда за всю серию.
    ace = SeekAce({0: (HF_YELLOW_BUF, 300, 800)},
                  occupied=(True, False, False, False))
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0)
    assert HF_YELLOW_UID in '\n'.join(cmd.responses)
    assert retracted(printer, 0) >= 300


def test_seek_stops_and_restores_if_the_slot_loses_the_filament():
    # Смотать оборот и вытащить пруток из подающих шестерён - разные вещи.
    # Как только гейт слота перестал видеть филамент, крутить дальше
    # нельзя: иначе катушку придётся заправлять руками.
    ace = SeekAce({}, occupied=(True, False, False, False))
    ace.lose_after = 200
    module, printer, ace = build_seek(ace)
    cmd = run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0)
    text = '\n'.join(cmd.responses)
    assert 'гейт' in text
    assert retracted(printer, 0) <= 300
    # И вернули ровно столько, сколько смотали.
    fed = sum(int(x.split('LENGTH=')[1].split()[0])
              for x in printer.lookup_object('gcode').scripts
              if x.startswith('ACE_FEED INDEX=0 '))
    assert fed == retracted(printer, 0)


def test_confirmed_tag_is_recorded_where_multiace_looks_for_it():
    # Одной привязки мало: веб-часть multiACE заводит катушку в Spoolman
    # по card_uids, опираясь на СОХРАНЁННЫЕ чтения меток, а не на строчку
    # в журнале. Без этой записи подтверждённая метка никуда не попадёт.
    ace = SeekAce({0: (HF_YELLOW_BUF, 0, 200), 1: (OTHER_BUF, 5000, 6000)})
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0)
    assert ace._rc_last_uid[(0, 0)] == HF_YELLOW_UID
    assert ace.persisted == 1


def test_unconfirmed_tag_is_not_recorded_anywhere():
    # Метка соседа не должна попасть ни в привязку, ни в сохранённые
    # чтения: иначе веб-часть заведёт её как нашу в обход проверки.
    ace = SeekAce({1: (OTHER_BUF, 0, 200)})
    module, printer, ace = build_seek(ace)
    run(printer, 'ACE_EXT_TAG_SEEK', ACE=0, SLOT=0)
    assert ace._rc_last_uid == {}
    assert ace.persisted == 0
