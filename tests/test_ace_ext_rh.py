import sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'printer'))

from fakes import (AceWithout, FakeAce, FakeConfig, FakeGcodeCommand,
                   FakeGcodeError, FakePrinter)
import ace_ext_rh


def build(ace=None, **cfg):
    printer = FakePrinter(ace=ace if ace is not None else FakeAce())
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, cfg))
    printer.fire('klippy:ready')
    return module, printer


def set_rh(module, printer, **params):
    cmd = FakeGcodeCommand({str(k): str(v) for k, v in params.items()})
    printer.lookup_object('gcode').commands['ACE_SET_RH'](cmd)
    return cmd


def test_stores_reading_with_expiry():
    module, printer = build()
    set_rh(module, printer, ACE=0, RH=42.5, TTL=1800)
    now = printer.reactor.monotonic()
    assert module.readings[0]['rh'] == 42.5
    assert module.readings[0]['expires'] == now + 1800


def test_reading_is_fresh_before_ttl_and_stale_after():
    module, printer = build()
    set_rh(module, printer, ACE=0, RH=42.5, TTL=600)
    printer.reactor.advance(599)
    assert module.fresh_rh(0, printer.reactor.monotonic()) == 42.5
    printer.reactor.advance(2)
    assert module.fresh_rh(0, printer.reactor.monotonic()) is None


def test_missing_reading_is_stale():
    module, printer = build()
    assert module.fresh_rh(0, printer.reactor.monotonic()) is None


@pytest.mark.parametrize('bad', ['abc', '-5', '150'])
def test_rejects_out_of_range_humidity(bad):
    module, printer = build()
    with pytest.raises(FakeGcodeError):
        set_rh(module, printer, ACE=0, RH=bad)
    assert module.readings == {}


def test_uses_default_ttl_when_not_given():
    module, printer = build(default_ttl=900)
    set_rh(module, printer, ACE=0, RH=40)
    now = printer.reactor.monotonic()
    assert module.readings[0]['expires'] == now + 900


def test_missing_mandatory_rh_parameter_raises_error():
    module, printer = build()
    with pytest.raises(FakeGcodeError) as exc_info:
        set_rh(module, printer, ACE=0)
    assert 'missing RH' in str(exc_info.value)
    assert module.readings == {}


def test_default_ttl_out_of_range_raises_error():
    printer = FakePrinter(ace=FakeAce())
    with pytest.raises(ValueError):
        module = ace_ext_rh.AceExtRh(FakeConfig(printer, {'default_ttl': 30}))
    with pytest.raises(ValueError):
        module = ace_ext_rh.AceExtRh(FakeConfig(printer, {'default_ttl': 10000}))


def test_disabled_when_ace_object_missing():
    printer = FakePrinter(ace=None)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    assert module.ace is None
    assert 'multiACE' in module.disabled_reason


def test_disabled_when_required_method_renamed(caplog):
    """Переименование метода, нужного для ГАШЕНИЯ, отключает модуль целиком
    (для метода запуска действует более мягкий режим, см. ниже)."""
    ace = AceWithout(FakeAce(), '_auto_dry_for')
    printer = FakePrinter(ace=ace)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    with caplog.at_level('ERROR'):
        printer.fire('klippy:ready')
    assert module.ace is None
    assert '_auto_dry_for' in module.disabled_reason
    assert any('_auto_dry_for' in r.message for r in caplog.records)


def test_enabled_when_all_methods_present():
    module, printer = build()
    assert module.ace is not None
    assert module.disabled_reason is None


def enable_auto_dry(ace, idx=0, **over):
    cfg = {'enabled': True, 'master': idx, 'rh_start': 45., 'rh_end': 35.,
           'temp': 50}
    cfg.update(over)
    ace._auto_dry_cfg[str(idx)] = cfg


def test_starts_drying_above_threshold():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=50)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == [(0, 50, 'внешние 50%rH')]


def test_does_not_start_below_threshold():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=44)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_does_not_start_when_auto_dry_disabled():
    ace = FakeAce()
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_does_not_start_while_printing():
    ace = FakeAce(printing=True)
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_starts_while_printing_when_allowed():
    ace = FakeAce(printing=True)
    ace.auto_dry_while_printing = True
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert len(ace.started_calls) == 1


def test_does_not_restart_cycle_already_running():
    ace = FakeAce()
    enable_auto_dry(ace)
    ace._drying.add(0)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_tick_reschedules_itself():
    module, printer = build()
    assert module._tick(500.) == 500. + ace_ext_rh.EXT_RH_INTERVAL


def test_tick_survives_exception_from_ace(caplog):
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)

    def boom(idx, temp, why):
        raise RuntimeError('устройство отвалилось')
    ace._auto_dry_start = boom
    with caplog.at_level('ERROR'):
        assert module._tick(500.) == 500. + ace_ext_rh.EXT_RH_INTERVAL


def test_stops_when_humidity_reaches_lower_threshold():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    set_rh(module, printer, ACE=0, RH=34)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == [(0, 'внешние 34%rH')]


def test_keeps_drying_between_thresholds():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    set_rh(module, printer, ACE=0, RH=40)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == []


def test_stops_when_reading_goes_stale():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90, TTL=600)
    module._evaluate(printer.reactor.monotonic())
    printer.reactor.advance(601)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == [(0, 'показание влажности протухло')]


def test_stops_cycle_restored_after_klipper_restart():
    """После перезапуска показаний ещё нет, а цикл multiACE восстановил."""
    ace = FakeAce()
    enable_auto_dry(ace)
    ace._auto_dry_started.add(0)
    ace._drying.add(0)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == [(0, 'показание влажности протухло')]


def test_never_stops_a_manual_cycle():
    """Ручной ACE_DRY не попадает в _auto_dry_started и трогать его нельзя.
    RH=30 ниже rh_end: если бы _is_ours ошибочно посчитал цикл нашим, ветка
    остановки реально сработала бы и тест бы это поймал."""
    ace = FakeAce()
    enable_auto_dry(ace)
    ace._drying.add(0)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=30)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == []


def test_never_stops_a_real_ace2_cycle():
    ace = FakeAce(v2=True)
    enable_auto_dry(ace)
    ace._auto_dry_started.add(0)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == []


def test_never_stops_a_follower_of_another_master():
    ace = FakeAce()
    enable_auto_dry(ace, master=1)
    ace._auto_dry_started.add(0)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == []


def test_never_stops_unit_inside_follower_add_time():
    ace = FakeAce()
    enable_auto_dry(ace)
    ace._auto_dry_started.add(0)
    ace._auto_dry_follow_until[0] = 9999.
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == []


def test_stops_our_cycle_when_auto_dry_disabled_mid_run():
    """Человек снял галочку автосушки в multiACE, пока наш цикл греет:
    показания продолжают приходить свежими, но гасить всё равно надо —
    иначе 600 минут нагрева без присмотра."""
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert 0 in ace._drying
    ace._auto_dry_cfg['0']['enabled'] = False
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == [(0, 'автосушка выключена')]


def test_stale_stop_failure_does_not_block_other_devices(caplog):
    """Отказ _auto_dry_stop на одном устройстве (например, USB отвалился)
    не должен мешать погасить остальные протухшие/выключенные циклы в этом
    же тике."""
    ace = FakeAce()
    enable_auto_dry(ace, idx=0)
    enable_auto_dry(ace, idx=1)
    ace._auto_dry_started.update([0, 1])
    ace._drying.update([0, 1])
    real_stop = ace._auto_dry_stop

    def flaky_stop(idx, why):
        if idx == 0:
            raise RuntimeError('устройство 0 недоступно')
        real_stop(idx, why)
    ace._auto_dry_stop = flaky_stop
    module, printer = build(ace=ace)
    with caplog.at_level('ERROR'):
        module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == [(1, 'показание влажности протухло')]


def call(printer, name, **params):
    cmd = FakeGcodeCommand({str(k): str(v) for k, v in params.items()})
    printer.lookup_object('gcode').commands[name](cmd)
    return cmd


def test_enable_writes_config_with_self_master():
    ace = FakeAce()
    module, printer = build(ace=ace)
    call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1)
    cfg = ace._auto_dry_cfg['0']
    assert cfg['enabled'] is True
    assert cfg['master'] == 0
    assert ace.saved['ace__auto_dry'] == ace._auto_dry_cfg


def test_enable_accepts_thresholds():
    ace = FakeAce()
    module, printer = build(ace=ace)
    call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1, RH_START=50,
         RH_END=30, TEMP=45)
    cfg = ace._auto_dry_cfg['0']
    assert (cfg['rh_start'], cfg['rh_end'], cfg['temp']) == (50., 30., 45)


def test_enable_rejects_inverted_thresholds():
    ace = FakeAce()
    module, printer = build(ace=ace)
    with pytest.raises(FakeGcodeError):
        call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1, RH_START=30,
             RH_END=40)
    assert ace._auto_dry_cfg == {}


def test_enable_accepts_temp_up_to_printer_max():
    """Потолок TEMP берётся из max_dryer_temperature конкретного принтера,
    а не жёстко из умолчания multiACE (55) — иначе нельзя выразить уже
    сохранённую владельцем настройку выше 55."""
    ace = FakeAce()
    ace.max_dryer_temperature = 70
    module, printer = build(ace=ace)
    call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1, TEMP=60)
    assert ace._auto_dry_cfg['0']['temp'] == 60


def test_enable_rejects_temp_above_printer_max():
    ace = FakeAce()
    ace.max_dryer_temperature = 70
    module, printer = build(ace=ace)
    with pytest.raises(FakeGcodeError):
        call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1, TEMP=71)
    assert ace._auto_dry_cfg == {}


def test_enable_falls_back_to_conservative_max_when_attr_missing():
    """Если у multiACE вовсе нет атрибута max_dryer_temperature (старая
    сборка), действует консервативный запасной потолок 55, а не отказ."""
    ace = AceWithout(FakeAce(), 'max_dryer_temperature')
    module, printer = build(ace=ace)
    with pytest.raises(FakeGcodeError):
        call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1, TEMP=60)
    assert ace._auto_dry_cfg == {}


def test_disable_stops_running_cycle():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=0)
    assert ace._auto_dry_cfg['0']['enabled'] is False
    assert ace.stopped_calls == [(0, 'автосушка выключена')]


def test_status_reports_reading_and_age():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=42.5, TTL=1800)
    printer.reactor.advance(120)
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    text = '\n'.join(cmd.responses)
    assert '42.5' in text and '120' in text


def test_status_reports_disabled_reason():
    printer = FakePrinter(ace=None)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    assert 'multiACE' in '\n'.join(cmd.responses)


def test_enable_validates_thresholds_against_merged_config():
    """Пороги проверяются на слиянии действующей и присланной настроек.
    Первый вызов задаёт оба порога: RH_START=50, RH_END=30.
    Второй прислёт только RH_END=60 — на слиянии получится рh_start=50,
    rh_end=60, что нарушает условие rh_end < rh_start. Должно быть отвергнуто,
    и действующая rh_end должна остаться 30."""
    ace = FakeAce()
    module, printer = build(ace=ace)
    call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1, RH_START=50,
         RH_END=30)
    assert ace._auto_dry_cfg['0']['rh_end'] == 30.
    with pytest.raises(FakeGcodeError):
        call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1, RH_END=60)
    assert ace._auto_dry_cfg['0']['rh_end'] == 30.


def test_enable_fails_when_ace_disabled():
    """Команда должна отвергнуться, если модуль отключен, с причиной в ошибке."""
    printer = FakePrinter(ace=None)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    with pytest.raises(FakeGcodeError) as exc_info:
        call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1)
    assert 'multiACE' in str(exc_info.value)


# --- Правка 1: остановка подтверждается, владение не теряется зря ---------


def tick_n(module, printer, n, seconds=ace_ext_rh.EXT_RH_INTERVAL):
    """n тиков модуля с продвижением монотонного времени, как в реакторе."""
    for _ in range(n):
        module._evaluate(printer.reactor.monotonic())
        printer.reactor.advance(seconds)


def owned_stale(stop_obeyed=True):
    """Наш цикл греет, показаний нет вовсе — сушку надо погасить."""
    ace = FakeAce()
    ace.stop_obeyed = stop_obeyed
    enable_auto_dry(ace)
    ace._auto_dry_started.add(0)
    ace._drying.add(0)
    return ace


def test_repeats_stop_while_device_reports_drying():
    """Устройство не послушалось (ответ с ошибкой, потерянный пакет на USB):
    владение multiACE снял сразу, но нагреватель работает свои 600 минут.
    Единственный, кто может это исправить, — наш тик, значит он обязан
    повторять остановку, а не считать дело сделанным."""
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    tick_n(module, printer, 3)
    assert len(ace.stopped_calls) == 3
    assert all(c[0] == 0 for c in ace.stopped_calls)


def test_stop_repeats_end_after_device_confirms():
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    tick_n(module, printer, 1)
    assert len(ace.stopped_calls) == 1
    ace._drying.discard(0)          # устройство наконец прекратило сушку
    ace.stop_obeyed = True
    tick_n(module, printer, 4)
    assert len(ace.stopped_calls) == 1
    assert module.stop_pending == {}


def test_loud_error_after_stop_retries_exhausted(caplog):
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    # Тик 1 — само требование остановки, тики 2..STOP_RETRY_TICKS+1 —
    # быстрые повторы, и только следующий тик означает исчерпание.
    with caplog.at_level('ERROR'):
        tick_n(module, printer, ace_ext_rh.STOP_RETRY_TICKS + 2)
    errors = [r for r in caplog.records if r.levelname == 'ERROR']
    assert errors, 'после исчерпания повторов нужна запись уровня ERROR'
    assert 'НЕ ПОДТВЕРДИЛ ОСТАНОВКУ' in errors[0].message


def test_no_error_before_stop_retries_exhausted(caplog):
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    with caplog.at_level('ERROR'):
        tick_n(module, printer, ace_ext_rh.STOP_RETRY_TICKS + 1)
    assert [r for r in caplog.records if r.levelname == 'ERROR'] == []


def test_stop_demand_survives_exception_from_ace_stop():
    """Если сам вызов остановки бросил исключение, требование должно
    остаться записанным — иначе повторять будет некому."""
    ace = owned_stale(stop_obeyed=False)

    def boom(idx, why):
        raise RuntimeError('USB отвалился')
    ace._auto_dry_stop = boom
    module, printer = build(ace=ace)
    tick_n(module, printer, 1)
    assert 0 in module.stop_pending


# --- Правка 5: исчерпание 600-минутного предела --------------------------


def test_releases_ownership_when_device_stopped_by_itself():
    """Влажность не дошла до порога остановки, устройство отработало свои
    600 минут и погасло само. Молча считать цикл своим нельзя: ветка
    остановки только гасит, ветка старта требует «не наш» — автоматика
    залипла бы навсегда."""
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90, TTL=7200)
    module._evaluate(printer.reactor.monotonic())
    assert 0 in ace._auto_dry_started
    ace._drying.discard(0)          # предел ACE_DRY исчерпан
    tick_n(module, printer, ace_ext_rh.IDLE_TICKS_TO_RELEASE)
    assert 0 not in ace._auto_dry_started


def test_does_not_release_ownership_on_single_idle_tick():
    """_ace_is_drying читает кэш опроса устройства и отстаёт от команды:
    отдавать владение с первого же тика значило бы терять свежий цикл."""
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90, TTL=7200)
    module._evaluate(printer.reactor.monotonic())
    ace._drying.discard(0)
    tick_n(module, printer, ace_ext_rh.IDLE_TICKS_TO_RELEASE - 1)
    assert 0 in ace._auto_dry_started


def test_automation_recovers_after_ownership_released():
    """Отдав владение, автоматика снова может запустить сушку — влажность
    всё ещё выше порога старта."""
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90, TTL=7200)
    module._evaluate(printer.reactor.monotonic())
    ace._drying.discard(0)
    tick_n(module, printer, ace_ext_rh.IDLE_TICKS_TO_RELEASE + 2)
    assert len(ace.started_calls) == 2


# --- Правка 2: ветка запуска повторяет условия владения ------------------


def test_does_not_start_on_real_ace2():
    """На ACE 2 цикл запускать нельзя: _is_ours его нашим не считает, и
    погасить его мы потом не сможем."""
    ace = FakeAce(v2=True)
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_does_not_start_follower_of_another_master():
    """Мастер — другое устройство: уборка осиротевших в multiACE погасит
    такой цикл в своём же тике, и нагрев замигает раз в минуту."""
    ace = FakeAce()
    enable_auto_dry(ace, master=1)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_does_not_start_inside_follower_add_time_window():
    ace = FakeAce()
    enable_auto_dry(ace)
    ace._auto_dry_follow_until[0] = 9999.
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_does_not_start_disconnected_device():
    """Собственный тик multiACE тоже пропускает неподключённые устройства."""
    ace = FakeAce(connected=False)
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == []


def test_connected_attr_is_required():
    assert '_connected_per_ace' in ace_ext_rh.REQUIRED_ACE_ATTRS


# --- Правка 3: режим «только гашу» --------------------------------------


def test_stop_only_mode_when_start_attr_missing(caplog):
    """Отсутствует только то, что нужно для ЗАПУСКА: отключаться полностью
    нельзя — цикл, запущенный нами до обновления multiACE, восстановится из
    хранилища, и гасить его будет некому."""
    ace = AceWithout(FakeAce(), '_auto_dry_start')
    printer = FakePrinter(ace=ace)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    with caplog.at_level('INFO'):
        printer.fire('klippy:ready')
    assert module.ace is not None
    assert module.disabled_reason is None
    assert '_auto_dry_start' in module.stop_only_reason
    assert module.timer is not None
    assert any('готов' in r.message for r in caplog.records)


def test_stop_only_mode_still_stops_our_cycle():
    inner = owned_stale()
    ace = AceWithout(inner, '_auto_dry_start')
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    assert inner.stopped_calls == [(0, 'показание влажности протухло')]


def test_stop_only_mode_does_not_start_new_cycles():
    inner = FakeAce()
    enable_auto_dry(inner)
    ace = AceWithout(inner, '_auto_dry_start')
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert inner.started_calls == []


def test_disabled_completely_when_stop_attr_missing(caplog):
    ace = AceWithout(FakeAce(), '_auto_dry_stop')
    printer = FakePrinter(ace=ace)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    with caplog.at_level('ERROR'):
        printer.fire('klippy:ready')
    assert module.ace is None
    assert '_auto_dry_stop' in module.disabled_reason
    assert module.timer is None


def test_status_reports_stop_only_mode():
    ace = AceWithout(FakeAce(), '_auto_dry_start')
    module, printer = build(ace=ace)
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    assert 'ТОЛЬКО ГАШЕНИЕ' in '\n'.join(cmd.responses)


# --- Правка 4: температура датчика видна владельцу ----------------------


def test_status_reports_sensor_temperature_and_its_age():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=42.5, TEMP=41.3)
    printer.reactor.advance(90)
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    text = '\n'.join(cmd.responses)
    assert '41.3' in text and '90' in text


def test_status_warns_when_sensor_temperature_near_limit():
    """Датчик паспортно рассчитан до 60 C, а сушка у владельца — 60 C."""
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=42.5, TEMP=58.9)
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    text = '\n'.join(cmd.responses)
    assert 'ВНИМАНИЕ' in text and '58.9' in text


def test_status_does_not_warn_below_temperature_limit():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=42.5, TEMP=45.)
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    assert 'ВНИМАНИЕ' not in '\n'.join(cmd.responses)


def test_status_survives_reading_without_temperature():
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=42.5)
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    assert '42.5' in '\n'.join(cmd.responses)


# --- Правка 6: определение владения внутри защищённого блока -------------


def test_sweep_survives_exception_from_ownership_check(caplog):
    """_is_ours обращается к чужому объекту: исключение оттуда не должно
    обрывать уборку для ОСТАЛЬНЫХ устройств — это ровно тот сценарий
    пропавшего USB, ради которого защита и написана."""
    ace = FakeAce()
    enable_auto_dry(ace, idx=0)
    enable_auto_dry(ace, idx=1)
    ace._auto_dry_started.update([0, 1])
    ace._drying.update([0, 1])

    def boom(idx):
        if idx == 0:
            raise RuntimeError('устройство 0 недоступно')
        return False
    ace._is_v2 = boom
    module, printer = build(ace=ace)
    with caplog.at_level('ERROR'):
        module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == [(1, 'показание влажности протухло')]


# --- Правка 9: температура запуска ограничена потолком устройства --------


def test_start_temperature_clamped_to_device_ceiling():
    """Владелец понизил max_dryer_temperature — сохранённая температура
    автосушки выше потолка греть больше не должна."""
    ace = FakeAce()
    ace.max_dryer_temperature = 45
    enable_auto_dry(ace, temp=60)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == [(0, 45, 'внешние 90%rH')]


def test_start_temperature_kept_below_ceiling():
    ace = FakeAce()
    ace.max_dryer_temperature = 70
    enable_auto_dry(ace, temp=60)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    assert ace.started_calls == [(0, 60, 'внешние 90%rH')]


# --- Правка 11: уборка выполняется первой -------------------------------


def test_sweep_runs_before_threshold_loop():
    """Инвариант: уборка идёт ДО цикла порогов. Отказ запуска на устройстве 0
    прерывает цикл порогов целиком — протухший цикл устройства 1 к этому
    моменту должен быть уже погашен."""
    ace = FakeAce()
    enable_auto_dry(ace, idx=0)
    enable_auto_dry(ace, idx=1)
    ace._auto_dry_started.add(1)
    ace._drying.add(1)

    def boom(idx, temp, why):
        raise RuntimeError('устройство 0 отвалилось при запуске')
    ace._auto_dry_start = boom
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    with pytest.raises(RuntimeError):
        module._evaluate(printer.reactor.monotonic())
    assert ace.stopped_calls == [(1, 'показание влажности протухло')]


# --- Пункт A: требование остановки переживает перезапуск Klipper ----------


def test_stop_demand_survives_klipper_restart():
    """multiACE снятие владения СОХРАНЯЕТ на диск, а требование остановки
    жило только в памяти экземпляра. Перезапуск (а его делает и
    FIRMWARE_RESTART, и аварийный останов) стирал память: владения нет,
    требования нет, устройство продолжает сушить 600 минут — повторять
    некому."""
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    assert len(ace.stopped_calls) == 1
    assert ace.saved[ace_ext_rh.STOP_PENDING_VAR] == [0]

    # Перезапуск Klipper: новый экземпляр модуля, память пуста, владение
    # multiACE уже снял и сохранил.
    fresh = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    assert 0 in fresh.stop_pending
    tick_n(fresh, printer, 2)
    assert len(ace.stopped_calls) >= 2, 'повторы обязаны продолжиться'


def test_restored_demand_is_forgotten_after_confirmation():
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    fresh = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    ace._drying.discard(0)
    ace.stop_obeyed = True
    tick_n(fresh, printer, ace_ext_rh.STOP_CONFIRM_TICKS + 1)
    assert fresh.stop_pending == {}
    assert ace.saved[ace_ext_rh.STOP_PENDING_VAR] == []


def test_stored_demand_keys_are_normalised():
    """Хранилище переживает перезапуск в сериализованном виде: на индексы,
    пришедшие строками, полагаться нельзя."""
    ace = FakeAce()
    ace.saved[ace_ext_rh.STOP_PENDING_VAR] = ['0', 1]
    ace._drying.update([0, 1])
    printer = FakePrinter(ace=ace)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    assert sorted(module.stop_pending) == [0, 1]


def test_garbage_in_storage_does_not_break_startup():
    ace = FakeAce()
    ace.saved[ace_ext_rh.STOP_PENDING_VAR] = 'мусор'
    printer = FakePrinter(ace=ace)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    assert module.stop_pending == {}
    assert module.timer is not None


def test_stop_demand_is_not_persisted_with_percent_in_reason():
    """В хранилище уходят только индексы. Текст причины содержит знак
    процента ('внешние 34%rH'), а SAVE_VARIABLE гонит весь файл переменных
    через configparser с интерполяцией и на таком значении падает, унося
    запись переменных multiACE вместе с нашими."""
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90)
    module._evaluate(printer.reactor.monotonic())
    set_rh(module, printer, ACE=0, RH=34)
    module._evaluate(printer.reactor.monotonic())
    saved = ace.saved[ace_ext_rh.STOP_PENDING_VAR]
    assert saved == [0]
    assert '%' not in repr(saved)


# --- Пункт B: счётчик простоя чистится при любой потере владения ----------


def test_idle_counter_cleared_when_ownership_lost_outside_our_paths():
    """Ручная остановка сушилки уводит владение вне наших путей. Подвисший
    счётчик обваливает трёхтиковый запас до одного тика: следующий, только
    что запущенный нами цикл гасится на первой же задержке кэша, да ещё и с
    ложной записью «не сушит 3 тика подряд»."""
    ace = FakeAce()
    enable_auto_dry(ace)
    module, printer = build(ace=ace)
    set_rh(module, printer, ACE=0, RH=90, TTL=7200)
    module._evaluate(printer.reactor.monotonic())
    ace._drying.discard(0)
    tick_n(module, printer, ace_ext_rh.IDLE_TICKS_TO_RELEASE - 1)
    assert module.idle_ticks.get(0) == ace_ext_rh.IDLE_TICKS_TO_RELEASE - 1

    # Человек остановил сушку в multiACE: владение ушло не нашим путём.
    ace._auto_dry_started.discard(0)
    stops_before = len(ace.stopped_calls)
    module._evaluate(printer.reactor.monotonic())   # уборка чистит счётчик
    assert module.idle_ticks == {}
    assert 0 in ace._auto_dry_started, 'цикл должен быть запущен заново'

    # Кэш статуса ещё не догнал запуск — один тик «не сушит» не должен
    # отдавать владение.
    ace._drying.discard(0)
    printer.reactor.advance(ace_ext_rh.EXT_RH_INTERVAL)
    module._evaluate(printer.reactor.monotonic())
    assert 0 in ace._auto_dry_started
    assert len(ace.stopped_calls) == stops_before


# --- Пункт C: незакрытое требование видно и без показаний ----------------


def test_status_shows_unconfirmed_stop_when_no_readings_yet():
    """Самый частый путь появления требования — протухание сразу после
    перезапуска Klipper, когда показаний ещё не было ни одного."""
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    assert module.readings == {}
    cmd = call(printer, 'ACE_EXT_RH_STATUS')
    text = '\n'.join(cmd.responses)
    assert 'не подтвердил остановку' in text
    assert 'показаний ещё не поступало' in text


# --- Пункт D: группа «для настройки» деградирует команду, а не тик -------


def test_config_attrs_are_not_in_stop_group():
    for attr in ('_auto_dry_cfg', 'auto_dry_default'):
        assert attr not in ace_ext_rh.REQUIRED_ACE_ATTRS_STOP
        assert attr in ace_ext_rh.REQUIRED_ACE_ATTRS
    assert 'save_variable' in ace_ext_rh.REQUIRED_ACE_ATTRS_STOP


def test_missing_config_attr_keeps_tick_and_stops_our_cycle():
    inner = owned_stale()
    ace = AceWithout(inner, '_auto_dry_cfg')
    module, printer = build(ace=ace)
    assert module.ace is not None
    assert module.timer is not None
    assert '_auto_dry_cfg' in module.no_config_reason
    module._evaluate(printer.reactor.monotonic())
    assert inner.stopped_calls == [(0, 'показание влажности протухло')]


def test_missing_config_attr_degrades_enable_command():
    ace = AceWithout(FakeAce(), 'auto_dry_default')
    module, printer = build(ace=ace)
    with pytest.raises(FakeGcodeError) as exc_info:
        call(printer, 'ACE_EXT_RH_ENABLE', ACE=0, ENABLE=1)
    assert 'auto_dry_default' in str(exc_info.value)


# --- Пункт E: подтверждение осторожное ----------------------------------


def test_confirmation_needs_two_consecutive_quiet_ticks():
    """Кадр без блока сушилки и первые секунды после переподключения
    читаются как «не сушу», хотя это неправда."""
    ace = owned_stale(stop_obeyed=True)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())   # требование, устройство стихло
    printer.reactor.advance(ace_ext_rh.EXT_RH_INTERVAL)
    module._evaluate(printer.reactor.monotonic())   # первый тихий тик
    assert 0 in module.stop_pending
    printer.reactor.advance(ace_ext_rh.EXT_RH_INTERVAL)
    module._evaluate(printer.reactor.monotonic())   # второй тихий тик
    assert module.stop_pending == {}


def test_single_quiet_tick_does_not_confirm_and_flip_back_resets_it():
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    ace._drying.discard(0)
    tick_n(module, printer, 1)                     # один тихий тик
    assert 0 in module.stop_pending
    ace._drying.add(0)                             # кадр опять говорит «сушу»
    tick_n(module, printer, 1)
    assert module.stop_pending[0]['clear'] == 0
    assert len(ace.stopped_calls) >= 2


def test_disconnected_device_cannot_confirm_stop():
    """Первые секунды после переподключения устройство отвечает «не сушу»,
    хотя нагрев идёт. Подтверждение от неподключённого устройства не
    принимается."""
    ace = owned_stale(stop_obeyed=False)
    module, printer = build(ace=ace)
    module._evaluate(printer.reactor.monotonic())
    ace._drying.discard(0)                         # ответ «не сушу»...
    ace._connected_per_ace[0] = False              # ...но связи нет
    tick_n(module, printer, 3)
    assert 0 in module.stop_pending, 'требование снимать нельзя'
    assert len(ace.stopped_calls) >= 3
