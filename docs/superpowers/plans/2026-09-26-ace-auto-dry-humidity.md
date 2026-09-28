# Автосушка ACE Pro по внешнему датчику влажности — план реализации

> **Для агентов-исполнителей:** ОБЯЗАТЕЛЬНЫЙ ПОД-СКИЛЛ: используйте
> superpowers:subagent-driven-development (рекомендуется) или
> superpowers:executing-plans для выполнения плана задача за задачей.
> Шаги размечены чекбоксами (`- [ ]`).

**Цель:** ACE Pro сам включает и выключает сушку по показаниям внешнего
BLE-датчика влажности, используя штатный механизм автосушки multiACE.

**Архитектура:** Модуль Klipper `ace_ext_rh.py` на принтере принимает влажность
G-code командой и вызывает приватные методы объекта `ace` (`_auto_dry_start` /
`_auto_dry_stop`), снимая единственное ограничение — проверку «только ACE 2».
Сервис `ace-rh-feeder` на сервере читает датчик по BLE и раз в 5 минут шлёт
значение в Moonraker. Файл `ace.py` не изменяется.

**Стек:** Python 3.11 (принтер, только стандартная библиотека), Python 3.12 +
bleak (сервер, Docker), pytest (тесты на сервере), Moonraker HTTP API.

**Спека:** `docs/superpowers/specs/2026-09-26-ace-auto-dry-humidity-design.md`

## Глобальные ограничения

- Принтер: Python 3.11.8, pytest отсутствует. Модуль `ace_ext_rh.py` использует
  **только стандартную библиотеку** и импортируется без Klipper.
- `/home/lava/klipper/klippy/extras/ace.py` **не изменяется ни на строку**.
- Принтер: `<printer-ip>`, SSH root, пароль `<printer-ssh-password>`, dropbear.
- Moonraker: `http://<printer-ip>:7125`, без авторизации.
- Датчик: `AA:BB:CC:DD:EE:FF`, характеристика
  `ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6`, 5 байт `<hBH`:
  температура `int16/100` °C, влажность `uint8` %, батарея `uint16` мВ.
- Индекс ACE в командах: `0` (внутренний), отображается как «ACE 1».
- Диапазоны: `RH` 0..100, `TTL` 60..7200 с, `TEMP` 35..55 (`max_dryer_temperature`).
- Период тика модуля: 60 с (совпадает с `AUTO_DRY_INTERVAL` multiACE).
- Все сообщения в лог начинаются с префикса `[ace_ext_rh]`.
- Тесты запускаются на сервере: `cd "~" && .venv/bin/pytest`

## Зона внимания ревьюера

Пять условий, которые спека подразумевает, каждое закрыто тестом в своей задаче:

1. **Протухшие показания при активном цикле.** `_auto_dry_start` командует ACE с
   `duration=600` минут — устройство само не остановится 10 часов. Севшая
   батарейка или упавший сервер обязаны гасить нагрев. (Задача 4)
2. **Перезапуск Klipper с активным циклом.** multiACE восстанавливает
   `_auto_dry_started` из `save_variables`, а у модуля показаний ещё нет —
   цикл обязан быть погашен, а не оставлен греться вслепую. (Задача 4)
3. **Чужие циклы.** Ручной `ACE_DRY` и ведомый настоящего ACE 2 модуль не
   трогает никогда. (Задача 4)
4. **Несовместимая версия multiACE.** Переименование приватного метода после
   обновления не должно приводить к тихому бездействию — только громкая
   ошибка в логе и отключение. (Задача 2)
5. **Залипший BLE-адаптер.** RTL8761BU перестаёт отдавать LE-пакеты, оставаясь
   внешне исправным. Сервис обязан это распознать, перезапустить адаптер и
   **не отправлять устаревшее значение**. (Задача 9)

---

## Структура файлов

| Файл | Ответственность |
|---|---|
| `printer/ace_ext_rh.py` | модуль Klipper: приём влажности, тик, вызовы multiACE |
| `printer/ace_ext_rh.cfg` | секция `[ace_ext_rh]` для `extended/klipper/` |
| `tests/fakes.py` | подставные Klipper и `ace` для тестов модуля |
| `tests/test_ace_ext_rh.py` | тесты модуля |
| `feeder/ace_rh_feeder/sensor.py` | чтение LYWSD03MMC по BLE |
| `feeder/ace_rh_feeder/moonraker.py` | отправка G-code в Moonraker |
| `feeder/ace_rh_feeder/adapter.py` | перезапуск залипшего BLE-адаптера |
| `feeder/ace_rh_feeder/main.py` | главный цикл |
| `feeder/tests/test_*.py` | тесты сервиса |
| `feeder/Dockerfile`, `feeder/docker-compose.yml` | сборка и запуск |
| `scripts/psh.py` | выполнение команд на принтере по SSH |
| `scripts/deploy_printer.sh` | доставка модуля и конфига на принтер |

---

### Task 1: Приём влажности — хранилище и команда `ACE_SET_RH`

**Файлы:**
- Создать: `printer/ace_ext_rh.py`
- Создать: `tests/fakes.py`
- Создать: `tests/test_ace_ext_rh.py`
- Создать: `pytest.ini`

**Интерфейсы:**
- Отдаёт: класс `AceExtRh`, функция `load_config(config)`, метод
  `fresh_rh(idx, now) -> float | None`, словарь `readings[idx] = {'rh', 'temp',
  'at', 'expires'}`, константа `EXT_RH_INTERVAL = 60.0`.

- [ ] **Шаг 1: Создать окружение для тестов**

```bash
cd "~"
python3 -m venv .venv
.venv/bin/pip install -q pytest
printf '[pytest]\ntestpaths = tests feeder/tests\n' > pytest.ini
printf '.venv/\n__pycache__/\n*.pyc\n' > .gitignore
```

- [ ] **Шаг 2: Написать подставные объекты Klipper**

Создать `tests/fakes.py`:

```python
"""Подставные объекты Klipper и multiACE для тестов без принтера."""


class FakeReactor:
    NOW = 0.

    def __init__(self):
        self.time = 1000.
        self.timers = []

    def monotonic(self):
        return self.time

    def register_timer(self, callback, when):
        self.timers.append(callback)
        return callback

    def advance(self, seconds):
        self.time += seconds


class FakeGcodeCommand:
    """Повторяет поведение Klipper: отсутствующий обязательный параметр и
    выход за диапазон поднимают ошибку."""

    def __init__(self, params):
        self.params = params
        self.responses = []

    def _get(self, name, default, cast, minval, maxval):
        raw = self.params.get(name)
        if raw is None:
            if default is _SENTINEL:
                raise FakeGcodeError('missing %s' % name)
            return default
        try:
            val = cast(raw)
        except (TypeError, ValueError):
            raise FakeGcodeError('%s: "%s" is not a number' % (name, raw))
        if minval is not None and val < minval:
            raise FakeGcodeError('%s below %s' % (name, minval))
        if maxval is not None and val > maxval:
            raise FakeGcodeError('%s above %s' % (name, maxval))
        return val

    def get_int(self, name, default=None, minval=None, maxval=None):
        return self._get(name, default, int, minval, maxval)

    def get_float(self, name, default=None, minval=None, maxval=None):
        return self._get(name, default, float, minval, maxval)

    def respond_info(self, msg):
        self.responses.append(msg)


class FakeGcodeError(Exception):
    pass


_SENTINEL = object()


class FakeGcode:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def error(self, msg):
        return FakeGcodeError(msg)


class FakeAce:
    """Объект multiACE в объёме, который использует ace_ext_rh."""

    def __init__(self, v2=False, printing=False):
        self._auto_dry_cfg = {}
        self._auto_dry_started = set()
        self._auto_dry_follow_until = {}
        self._drying = set()
        self._v2 = set([0]) if v2 else set()
        self._printing = printing
        self.auto_dry_while_printing = False
        self.auto_dry_default = {'enabled': False, 'rh_start': 45.,
                                 'rh_end': 35., 'temp': 50, 'add_time': 60}
        self.saved = {}
        self.started_calls = []
        self.stopped_calls = []

    def _auto_dry_for(self, idx):
        cfg = dict(self.auto_dry_default)
        cfg.update(self._auto_dry_cfg.get(str(idx), {}))
        return cfg

    def _is_v2(self, idx):
        return idx in self._v2

    def _ace_is_drying(self, idx):
        return idx in self._drying

    def _is_actively_printing(self):
        return self._printing

    def _auto_dry_start(self, idx, temp, why):
        self.started_calls.append((idx, temp, why))
        self._auto_dry_started.add(idx)
        self._drying.add(idx)

    def _auto_dry_stop(self, idx, why):
        self.stopped_calls.append((idx, why))
        self._auto_dry_started.discard(idx)
        self._drying.discard(idx)

    def save_variable(self, name, value, write=False):
        self.saved[name] = value


class FakeConfig:
    def __init__(self, printer, values=None):
        self._printer = printer
        self._values = values or {}

    def get_printer(self):
        return self._printer

    def getint(self, name, default, minval=None, maxval=None):
        return int(self._values.get(name, default))


class FakePrinter:
    def __init__(self, ace=None):
        self.reactor = FakeReactor()
        self.objects = {'gcode': FakeGcode()}
        if ace is not None:
            self.objects['ace'] = ace
        self.event_handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=_SENTINEL):
        if name in self.objects:
            return self.objects[name]
        if default is _SENTINEL:
            raise KeyError(name)
        return default

    def register_event_handler(self, event, handler):
        self.event_handlers[event] = handler

    def fire(self, event):
        self.event_handlers[event]()
```

- [ ] **Шаг 3: Написать падающие тесты приёма влажности**

Создать `tests/test_ace_ext_rh.py`:

```python
import sys, os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'printer'))

from fakes import (FakeAce, FakeConfig, FakeGcodeCommand, FakeGcodeError,
                   FakePrinter)
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
```

- [ ] **Шаг 4: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -v`
Ожидается: `ModuleNotFoundError: No module named 'ace_ext_rh'`

- [ ] **Шаг 5: Написать минимальную реализацию**

Создать `printer/ace_ext_rh.py`:

```python
# Внешний источник влажности для автосушки multiACE.
#
# ACE Pro не имеет датчика влажности, поэтому штатный механизм автосушки
# multiACE для него выключен. Этот модуль принимает показания внешнего
# датчика G-code командой и вызывает механизм multiACE напрямую.
# Файл ace.py при этом не изменяется и спокойно обновляется.
import logging

EXT_RH_INTERVAL = 60.0
DEFAULT_TTL = 1800
MIN_TTL = 60
MAX_TTL = 7200


class AceExtRh:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.default_ttl = config.getint('default_ttl', DEFAULT_TTL,
                                         minval=MIN_TTL, maxval=MAX_TTL)
        self.readings = {}
        self.ace = None
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('ACE_SET_RH', self.cmd_ACE_SET_RH,
                               desc=self.cmd_ACE_SET_RH_help)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _handle_ready(self):
        self.ace = self.printer.lookup_object('ace', None)

    cmd_ACE_SET_RH_help = (
        '[ace_ext_rh] Подать влажность извне: ACE_SET_RH ACE=0 RH=42.5 '
        '[TTL=1800] [TEMP=]. Показание живёт TTL секунд, после чего '
        'считается протухшим и гасит нагрев.')

    def cmd_ACE_SET_RH(self, gcmd):
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        rh = gcmd.get_float('RH', minval=0., maxval=100.)
        ttl = gcmd.get_int('TTL', self.default_ttl,
                           minval=MIN_TTL, maxval=MAX_TTL)
        temp = gcmd.get_float('TEMP', None, minval=-40., maxval=125.)
        now = self.reactor.monotonic()
        self.readings[idx] = {'rh': rh, 'temp': temp, 'at': now,
                              'expires': now + ttl}
        gcmd.respond_info('[ace_ext_rh] ACE %d: %.1f%%rH (годно %d с)'
                          % (idx + 1, rh, ttl))

    def fresh_rh(self, idx, now):
        r = self.readings.get(idx)
        if r is None or now >= r['expires']:
            return None
        return r['rh']


def load_config(config):
    return AceExtRh(config)
```

- [ ] **Шаг 6: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -v`
Ожидается: 7 passed

- [ ] **Шаг 7: Зафиксировать**

```bash
git add pytest.ini .gitignore printer/ace_ext_rh.py tests/
git commit -m "feat: приём внешней влажности командой ACE_SET_RH"
```

---

### Task 2: Проверка совместимости с multiACE при старте

**Файлы:**
- Изменить: `printer/ace_ext_rh.py`
- Изменить: `tests/test_ace_ext_rh.py`

**Интерфейсы:**
- Потребляет: `AceExtRh`, `FakeAce` из задачи 1.
- Отдаёт: константу `REQUIRED_ACE_ATTRS`, поле `self.disabled_reason:
  str | None`, метод `_check_compatibility(ace) -> str | None`.

Модуль опирается на приватные методы multiACE. Обновление может их
переименовать — молчаливое бездействие после такого обновления недопустимо.

- [ ] **Шаг 1: Написать падающие тесты**

Добавить в `tests/test_ace_ext_rh.py`:

```python
def test_disabled_when_ace_object_missing():
    printer = FakePrinter(ace=None)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    printer.fire('klippy:ready')
    assert module.ace is None
    assert 'multiACE' in module.disabled_reason


def test_disabled_when_required_method_renamed(caplog):
    ace = FakeAce()
    del ace._auto_dry_start
    printer = FakePrinter(ace=ace)
    module = ace_ext_rh.AceExtRh(FakeConfig(printer, {}))
    with caplog.at_level('ERROR'):
        printer.fire('klippy:ready')
    assert module.ace is None
    assert '_auto_dry_start' in module.disabled_reason
    assert any('_auto_dry_start' in r.message for r in caplog.records)


def test_enabled_when_all_methods_present():
    module, printer = build()
    assert module.ace is not None
    assert module.disabled_reason is None
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -k disabled -v`
Ожидается: FAIL, `AttributeError: 'AceExtRh' object has no attribute 'disabled_reason'`

- [ ] **Шаг 3: Реализовать проверку**

В `printer/ace_ext_rh.py` добавить константу после `MAX_TTL`:

```python
REQUIRED_ACE_ATTRS = (
    '_auto_dry_for', '_auto_dry_start', '_auto_dry_stop',
    '_auto_dry_started', '_auto_dry_follow_until', '_ace_is_drying',
    '_is_v2', '_is_actively_printing', 'auto_dry_while_printing',
    '_auto_dry_cfg', 'save_variable',
)
```

В `__init__` добавить перед `gcode = ...`:

```python
        self.disabled_reason = None
```

Заменить `_handle_ready`:

```python
    def _handle_ready(self):
        ace = self.printer.lookup_object('ace', None)
        reason = self._check_compatibility(ace)
        if reason is not None:
            self.ace = None
            self.disabled_reason = reason
            logging.error('[ace_ext_rh] ОТКЛЮЧЁН: %s. Автосушка по внешней '
                          'влажности работать не будет.' % reason)
            return
        self.ace = ace
        self.disabled_reason = None
        logging.info('[ace_ext_rh] готов, multiACE совместим')

    def _check_compatibility(self, ace):
        if ace is None:
            return 'объект multiACE не найден'
        missing = [a for a in REQUIRED_ACE_ATTRS if not hasattr(ace, a)]
        if missing:
            return ('multiACE несовместим, отсутствует: %s'
                    % ', '.join(missing))
        return None
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -v`
Ожидается: 10 passed

- [ ] **Шаг 5: Зафиксировать**

```bash
git add printer/ace_ext_rh.py tests/test_ace_ext_rh.py
git commit -m "feat: отключение с громкой ошибкой при несовместимом multiACE"
```

---

### Task 3: Тик автосушки — запуск нагрева

**Файлы:**
- Изменить: `printer/ace_ext_rh.py`
- Изменить: `tests/test_ace_ext_rh.py`

**Интерфейсы:**
- Потребляет: `fresh_rh`, `self.ace`, `disabled_reason` из задач 1–2.
- Отдаёт: методы `_tick(eventtime) -> float` и `_evaluate(now)`, поле
  `self.timer`.

Пороги берутся из настроек multiACE через `ace._auto_dry_for(idx)` —
собственных порогов у модуля нет.

- [ ] **Шаг 1: Написать падающие тесты**

Добавить в `tests/test_ace_ext_rh.py`:

```python
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
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -k "start or tick" -v`
Ожидается: FAIL, `AttributeError: '_evaluate'`

- [ ] **Шаг 3: Реализовать тик**

В `_handle_ready` после `self.disabled_reason = None` добавить:

```python
        self.timer = self.reactor.register_timer(
            self._tick, self.reactor.NOW)
```

В `__init__` добавить `self.timer = None` рядом с `self.ace = None`.

Добавить методы:

```python
    def _tick(self, eventtime):
        try:
            self._evaluate(eventtime)
        except Exception as e:
            logging.exception('[ace_ext_rh] ошибка тика (пропущена): %s' % e)
        return eventtime + EXT_RH_INTERVAL

    def _evaluate(self, now):
        ace = self.ace
        if ace is None:
            return
        for idx in sorted(self.readings):
            rh = self.fresh_rh(idx, now)
            if rh is None:
                continue
            cfg = ace._auto_dry_for(idx)
            if not cfg.get('enabled'):
                continue
            ours = idx in ace._auto_dry_started
            if ours or ace._ace_is_drying(idx):
                continue
            if rh < float(cfg['rh_start']):
                continue
            if ace._is_actively_printing() and not ace.auto_dry_while_printing:
                continue
            ace._auto_dry_start(idx, int(cfg['temp']),
                                'внешние %.0f%%rH' % rh)
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -v`
Ожидается: 18 passed

- [ ] **Шаг 5: Зафиксировать**

```bash
git add printer/ace_ext_rh.py tests/test_ace_ext_rh.py
git commit -m "feat: запуск сушки по верхнему порогу влажности"
```

---

### Task 4: Тик — остановка нагрева, включая аварийную

**Файлы:**
- Изменить: `printer/ace_ext_rh.py`
- Изменить: `tests/test_ace_ext_rh.py`

**Интерфейсы:**
- Потребляет: `_evaluate` из задачи 3.
- Отдаёт: метод `_sweep_stale(now)`, вызываемый в начале `_evaluate`.

Самая ответственная часть. `_auto_dry_start` командует устройству
`duration=600` минут, поэтому остановка — единственное, что выключает
нагреватель.

- [ ] **Шаг 1: Написать падающие тесты**

Добавить в `tests/test_ace_ext_rh.py`:

```python
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
    """Ручной ACE_DRY не попадает в _auto_dry_started и трогать его нельзя."""
    ace = FakeAce()
    enable_auto_dry(ace)
    ace._drying.add(0)
    module, printer = build(ace=ace)
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
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -k stop -v`
Ожидается: FAIL, остановка не вызывается

- [ ] **Шаг 3: Реализовать остановку**

Заменить `_evaluate` целиком:

```python
    def _evaluate(self, now):
        ace = self.ace
        if ace is None:
            return
        self._sweep_stale(now)
        for idx in sorted(self.readings):
            rh = self.fresh_rh(idx, now)
            if rh is None:
                continue
            cfg = ace._auto_dry_for(idx)
            if not cfg.get('enabled'):
                continue
            ours = self._is_ours(idx)
            if ours:
                if rh <= float(cfg['rh_end']):
                    ace._auto_dry_stop(idx, 'внешние %.0f%%rH' % rh)
                continue
            if ace._ace_is_drying(idx):
                continue
            if rh < float(cfg['rh_start']):
                continue
            if ace._is_actively_printing() and not ace.auto_dry_while_printing:
                continue
            ace._auto_dry_start(idx, int(cfg['temp']),
                                'внешние %.0f%%rH' % rh)

    def _is_ours(self, idx):
        """Наш цикл — запущенный автосушкой на не-ACE 2, который назначен
        мастером самому себе. Ручной цикл в _auto_dry_started не попадает,
        ведомый настоящего ACE 2 указывает на другой мастер."""
        ace = self.ace
        if idx not in ace._auto_dry_started:
            return False
        if ace._is_v2(idx):
            return False
        if idx in ace._auto_dry_follow_until:
            return False
        return int(ace._auto_dry_for(idx).get('master', -1)) == idx

    def _sweep_stale(self, now):
        """Без свежих показаний нагрев гасится. _auto_dry_start командует
        устройству duration=600 минут: само оно не остановится."""
        ace = self.ace
        for idx in sorted(ace._auto_dry_started):
            if not self._is_ours(idx):
                continue
            if self.fresh_rh(idx, now) is None:
                ace._auto_dry_stop(idx, 'показание влажности протухло')
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -v`
Ожидается: 26 passed

- [ ] **Шаг 5: Зафиксировать**

```bash
git add printer/ace_ext_rh.py tests/test_ace_ext_rh.py
git commit -m "feat: остановка сушки по нижнему порогу и по протухшим данным"
```

---

### Task 5: Команды включения и состояния

**Файлы:**
- Изменить: `printer/ace_ext_rh.py`
- Изменить: `tests/test_ace_ext_rh.py`

**Интерфейсы:**
- Отдаёт: команды `ACE_EXT_RH_ENABLE ACE=0 ENABLE=1 [RH_START=] [RH_END=]
  [TEMP=]` и `ACE_EXT_RH_STATUS`.

Штатная `ACE_SET_AUTO_DRY` для ACE Pro принудительно сбрасывает `enabled` в
`False` при отсутствии мастера. Эта проверка живёт в обработчике команды, не в
движке, поэтому запись делается напрямую в словарь настроек.

- [ ] **Шаг 1: Написать падающие тесты**

Добавить в `tests/test_ace_ext_rh.py`:

```python
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
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -k "enable or status or disable" -v`
Ожидается: FAIL, `KeyError: 'ACE_EXT_RH_ENABLE'`

- [ ] **Шаг 3: Реализовать команды**

В `__init__` после регистрации `ACE_SET_RH` добавить:

```python
        gcode.register_command('ACE_EXT_RH_ENABLE', self.cmd_ACE_EXT_RH_ENABLE,
                               desc=self.cmd_ACE_EXT_RH_ENABLE_help)
        gcode.register_command('ACE_EXT_RH_STATUS', self.cmd_ACE_EXT_RH_STATUS,
                               desc=self.cmd_ACE_EXT_RH_STATUS_help)
```

Добавить методы:

```python
    cmd_ACE_EXT_RH_ENABLE_help = (
        '[ace_ext_rh] Включить автосушку по внешней влажности для ACE Pro: '
        'ACE_EXT_RH_ENABLE ACE=0 ENABLE=1 [RH_START=45] [RH_END=35] [TEMP=50]')

    def cmd_ACE_EXT_RH_ENABLE(self, gcmd):
        ace = self._require_ace(gcmd)
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        key = str(idx)
        cur = dict(ace._auto_dry_cfg.get(key, {}))
        cur['enabled'] = enable
        cur['master'] = idx
        for param, name, cast, lo, hi in (
                ('RH_START', 'rh_start', float, 5., 95.),
                ('RH_END', 'rh_end', float, 1., 94.),
                ('TEMP', 'temp', int, 35., 55.)):
            raw = gcmd.get_float(param, None, minval=lo, maxval=hi)
            if raw is not None:
                cur[name] = cast(raw)
        merged = dict(ace.auto_dry_default)
        merged.update(cur)
        if float(merged['rh_end']) >= float(merged['rh_start']):
            raise self.printer.lookup_object('gcode').error(
                '[ace_ext_rh] RH_END (%.0f) должен быть НИЖЕ RH_START (%.0f)'
                % (float(merged['rh_end']), float(merged['rh_start'])))
        ace._auto_dry_cfg[key] = cur
        try:
            ace.save_variable('ace__auto_dry', ace._auto_dry_cfg, write=True)
        except Exception as e:
            logging.info('[ace_ext_rh] не удалось сохранить настройки: %s' % e)
        if not enable and self._is_ours(idx):
            ace._auto_dry_stop(idx, 'автосушка выключена')
        gcmd.respond_info(
            '[ace_ext_rh] ACE %d: автосушка %s, старт >= %.0f%%, стоп <= '
            '%.0f%%, %d C' % (idx + 1, 'ВКЛ' if enable else 'ВЫКЛ',
                              float(merged['rh_start']),
                              float(merged['rh_end']), int(merged['temp'])))

    cmd_ACE_EXT_RH_STATUS_help = (
        '[ace_ext_rh] Показать внешнюю влажность, её возраст и состояние')

    def cmd_ACE_EXT_RH_STATUS(self, gcmd):
        if self.disabled_reason is not None:
            gcmd.respond_info('[ace_ext_rh] ОТКЛЮЧЁН: %s'
                              % self.disabled_reason)
            return
        now = self.reactor.monotonic()
        if not self.readings:
            gcmd.respond_info('[ace_ext_rh] показаний ещё не поступало')
            return
        for idx in sorted(self.readings):
            r = self.readings[idx]
            cfg = self.ace._auto_dry_for(idx)
            gcmd.respond_info(
                '[ace_ext_rh] ACE %d: %.1f%%rH, возраст %d с, %s | '
                'автосушка %s, старт >= %.0f%%, стоп <= %.0f%%, %d C | '
                'цикл наш: %s'
                % (idx + 1, r['rh'], int(now - r['at']),
                   'годно' if now < r['expires'] else 'ПРОТУХЛО',
                   'ВКЛ' if cfg.get('enabled') else 'ВЫКЛ',
                   float(cfg['rh_start']), float(cfg['rh_end']),
                   int(cfg['temp']), 'да' if self._is_ours(idx) else 'нет'))

    def _require_ace(self, gcmd):
        if self.ace is None:
            raise self.printer.lookup_object('gcode').error(
                '[ace_ext_rh] отключён: %s' % self.disabled_reason)
        return self.ace
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest tests/test_ace_ext_rh.py -v`
Ожидается: 32 passed

- [ ] **Шаг 5: Зафиксировать**

```bash
git add printer/ace_ext_rh.py tests/test_ace_ext_rh.py
git commit -m "feat: команды включения автосушки и просмотра состояния"
```

---

### Task 6: Доставка на принтер и живая проверка

**Файлы:**
- Создать: `printer/ace_ext_rh.cfg`
- Создать: `scripts/psh.py`
- Создать: `scripts/deploy_printer.sh`

**Интерфейсы:**
- Потребляет: `printer/ace_ext_rh.py` из задач 1–5.
- Отдаёт: рабочий модуль на принтере, команды доступны в Klipper.

- [ ] **Шаг 1: Создать конфиг и инструменты доставки**

Создать `printer/ace_ext_rh.cfg`:

```ini
# Внешний источник влажности для автосушки multiACE.
# Кладётся в printer_data/config/extended/klipper/ — этот каталог
# подключается строкой [include extended/klipper/*.cfg] в printer.cfg
# и не затрагивается обновлениями multiACE.
[ace_ext_rh]
# Срок годности показания по умолчанию, секунды. По истечении нагрев гасится.
default_ttl: 1800
```

Создать `scripts/psh.py`:

```python
"""Выполнить команды на принтере по SSH. Скрипт читается со stdin."""
import base64
import sys

import pexpect

HOST = "<printer-ip>"
PASSWORD = os.environ.get("PRINTER_SSH_PASSWORD", "")


def run(script, timeout=120):
    enc = base64.b64encode(script.encode()).decode()
    cmd = ("ssh -o StrictHostKeyChecking=accept-new "
           "-o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 "
           "root@%s 'echo %s | base64 -d | sh'" % (HOST, enc))
    child = pexpect.spawn(cmd, timeout=timeout, encoding='utf-8',
                          codec_errors='replace')
    if child.expect(['assword:', pexpect.EOF, pexpect.TIMEOUT]) == 0:
        child.sendline(PASSWORD)
    child.expect(pexpect.EOF, timeout=timeout)
    return child.before or ""


if __name__ == '__main__':
    print(run(sys.stdin.read()))
```

Создать `scripts/deploy_printer.sh`:

```bash
#!/usr/bin/env bash
# Доставка модуля и конфига на принтер с проверкой синтаксиса.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=<printer-ip>
PASS=<printer-ssh-password>
EXTRAS=/home/lava/klipper/klippy/extras
CFGDIR=/home/lava/printer_data/config/extended/klipper

python3 -c "import ast,sys; ast.parse(open('printer/ace_ext_rh.py').read())"
echo "синтаксис модуля в порядке"

for pair in "printer/ace_ext_rh.py:$EXTRAS/ace_ext_rh.py" \
            "printer/ace_ext_rh.cfg:$CFGDIR/ace_ext_rh.cfg"; do
    src="${pair%%:*}"; dst="${pair##*:}"
    python3 - "$src" "$dst" <<'PY'
import base64, sys
sys.path.insert(0, 'scripts')
from psh import run
src, dst = sys.argv[1], sys.argv[2]
data = base64.b64encode(open(src, 'rb').read()).decode()
print(run("echo %s | base64 -d > %s && wc -c %s" % (data, dst, dst)))
PY
done
echo "доставлено, требуется FIRMWARE_RESTART"
```

```bash
chmod +x scripts/deploy_printer.sh
```

- [ ] **Шаг 2: Сделать резервную копию конфигурации принтера**

```bash
python3 scripts/psh.py <<'EOF'
cp /home/lava/printer_data/config/printer.cfg \
   /home/lava/printer_data/config/printer.cfg.bak.ace_ext_rh
ls -l /home/lava/printer_data/config/printer.cfg.bak.ace_ext_rh
EOF
```

Ожидается: файл резервной копии существует.

- [ ] **Шаг 3: Доставить файлы и перезапустить Klipper**

```bash
./scripts/deploy_printer.sh
curl -s -X POST http://<printer-ip>:7125/printer/firmware_restart
sleep 25
curl -s http://<printer-ip>:7125/printer/info | head -c 200
```

Ожидается: `"state": "ready"`. Если `"state": "error"` — прочитать причину:

```bash
python3 scripts/psh.py <<'EOF'
tail -40 /oem/klippylogs/klippy.log
EOF
```

и откатиться: `cp printer.cfg.bak.ace_ext_rh printer.cfg`, удалить
`$EXTRAS/ace_ext_rh.py`, повторить `firmware_restart`.

- [ ] **Шаг 4: Проверить, что модуль загрузился и совместим**

```bash
python3 scripts/psh.py <<'EOF'
grep "ace_ext_rh" /oem/klippylogs/klippy.log | tail -5
EOF
```

Ожидается строка `[ace_ext_rh] готов, multiACE совместим`.
Если видно `ОТКЛЮЧЁН: multiACE несовместим, отсутствует: ...` — список
отсутствующих методов показывает, что переименовали в multiACE; обновить
`REQUIRED_ACE_ATTRS` и вызовы в модуле.

- [ ] **Шаг 5: Проверить команды вживую, без нагрева**

```bash
G() { curl -s -X POST http://<printer-ip>:7125/printer/gcode/script \
      -H 'Content-Type: application/json' -d "{\"script\":\"$1\"}"; }
S() { sleep 2; curl -s "http://<printer-ip>:7125/server/gcode_store?count=10" \
      | python3 -c "import json,sys;[print(m['message']) for m in json.load(sys.stdin)['result']['gcode_store'][-6:]]"; }

G "ACE_EXT_RH_STATUS"; S
G "ACE_SET_RH ACE=0 RH=42.5 TTL=1800"; S
G "ACE_EXT_RH_STATUS"; S
```

Ожидается: сначала «показаний ещё не поступало», затем приём `42.5%rH`,
затем строка состояния с возрастом в секундах и `цикл наш: нет`.
Порог старта 45 % не достигнут — **нагрев стартовать не должен**. Проверить:

```bash
curl -s "http://<printer-ip>:7125/printer/objects/query?ace" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['status']['ace']['dryer_status'])"
```

Ожидается: `{'status': 'stop', ...}`

- [ ] **Шаг 6: Зафиксировать**

```bash
git add printer/ace_ext_rh.cfg scripts/
git commit -m "feat: доставка модуля на принтер и живая проверка команд"
```

---

### Task 7: Сервис — чтение датчика по BLE

**Файлы:**
- Создать: `feeder/ace_rh_feeder/__init__.py`
- Создать: `feeder/ace_rh_feeder/sensor.py`
- Создать: `feeder/tests/test_sensor.py`

**Интерфейсы:**
- Отдаёт: `Reading(rh: float, temp_c: float, battery_mv: int)`,
  `decode(payload: bytes) -> Reading`, класс `XiaomiSensor(mac, adapter)`
  с методом `async read(timeout: float = 30.) -> Reading | None`.

- [ ] **Шаг 1: Установить зависимости и написать падающие тесты**

```bash
.venv/bin/pip install -q bleak pytest-asyncio
mkdir -p feeder/ace_rh_feeder feeder/tests
touch feeder/ace_rh_feeder/__init__.py feeder/tests/__init__.py
```

Создать `feeder/tests/test_sensor.py`:

```python
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
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest feeder/tests/test_sensor.py -v`
Ожидается: `ModuleNotFoundError: No module named 'ace_rh_feeder.sensor'`

- [ ] **Шаг 3: Реализовать чтение**

Создать `feeder/ace_rh_feeder/sensor.py`:

```python
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
    def __init__(self, mac, adapter='hci0'):
        self.mac = mac
        self.adapter = adapter

    async def read(self, timeout=30.):
        """Вернуть показание или None. Исключения не пробрасываются:
        недоступный датчик — штатная ситуация, а не сбой сервиса."""
        try:
            device = await BleakScanner.find_device_by_address(
                self.mac, timeout=timeout, adapter=self.adapter)
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

            async with BleakClient(device, timeout=timeout) as client:
                await client.start_notify(CHAR_UUID, on_data)
                try:
                    await asyncio.wait_for(done.wait(), timeout=timeout)
                finally:
                    await client.stop_notify(CHAR_UUID)
            return result.get('r')
        except Exception as e:
            log.warning('чтение датчика не удалось: %s', e)
            return None
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest feeder/tests/test_sensor.py -v`
Ожидается: 5 passed

- [ ] **Шаг 5: Проверить на живом датчике**

```bash
.venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, 'feeder')
from ace_rh_feeder.sensor import XiaomiSensor
print(asyncio.run(XiaomiSensor('AA:BB:CC:DD:EE:FF').read()))
"
```

Ожидается строка вида `Reading(rh=57.0, temp_c=26.95, battery_mv=3033)`.
Если `None` — адаптер залип, лечится `bluetoothctl power off && bluetoothctl
power on`; это и автоматизируется в задаче 9.

- [ ] **Шаг 6: Зафиксировать**

```bash
git add feeder/
git commit -m "feat: чтение влажности с Xiaomi LYWSD03MMC по BLE"
```

---

### Task 8: Сервис — отправка в Moonraker

**Файлы:**
- Создать: `feeder/ace_rh_feeder/moonraker.py`
- Создать: `feeder/tests/test_moonraker.py`

**Интерфейсы:**
- Отдаёт: класс `Moonraker(base_url, timeout=15., opener=None)` с методами
  `gcode(script) -> bool` и `set_rh(ace_index, rh, ttl, temp=None) -> bool`.

Только стандартная библиотека: `urllib.request`. Для тестов приём
`opener` — подставной обработчик запросов.

- [ ] **Шаг 1: Написать падающие тесты**

Создать `feeder/tests/test_moonraker.py`:

```python
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
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest feeder/tests/test_moonraker.py -v`
Ожидается: `ModuleNotFoundError: No module named 'ace_rh_feeder.moonraker'`

- [ ] **Шаг 3: Реализовать клиент**

Создать `feeder/ace_rh_feeder/moonraker.py`:

```python
"""Отправка G-code в Moonraker. Только стандартная библиотека."""
import json
import logging
import urllib.request

log = logging.getLogger(__name__)


class Moonraker:
    def __init__(self, base_url, timeout=15., opener=None):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def gcode(self, script):
        """Вернуть True при успехе. Недоступный принтер — не повод падать."""
        url = '%s/printer/gcode/script' % self.base_url
        data = json.dumps({'script': script}).encode()
        request = urllib.request.Request(
            url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with self._open(request, timeout=self.timeout) as response:
                response.read()
            return True
        except Exception as e:
            log.warning('Moonraker недоступен (%s): %s', script, e)
            return False

    def set_rh(self, ace_index, rh, ttl, temp=None):
        script = 'ACE_SET_RH ACE=%d RH=%.1f TTL=%d' % (ace_index, rh, ttl)
        if temp is not None:
            script += ' TEMP=%.1f' % temp
        return self.gcode(script)
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest feeder/tests/test_moonraker.py -v`
Ожидается: 4 passed

- [ ] **Шаг 5: Зафиксировать**

```bash
git add feeder/ace_rh_feeder/moonraker.py feeder/tests/test_moonraker.py
git commit -m "feat: клиент Moonraker для отправки влажности"
```

---

### Task 9: Сервис — главный цикл и восстановление адаптера

**Файлы:**
- Создать: `feeder/ace_rh_feeder/adapter.py`
- Создать: `feeder/ace_rh_feeder/main.py`
- Создать: `feeder/tests/test_main.py`

**Интерфейсы:**
- Потребляет: `XiaomiSensor.read`, `Moonraker.set_rh` из задач 7–8.
- Отдаёт: `reset_adapter(name='hci0', runner=None) -> bool`,
  `Feeder(sensor, moonraker, ace_index, ttl, resetter)` с методом
  `run_once() -> bool`, счётчиком `consecutive_failures`.

RTL8761BU перестаёт отдавать LE-пакеты, оставаясь внешне исправным.

- [ ] **Шаг 1: Написать падающие тесты**

Создать `feeder/tests/test_main.py`:

```python
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
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

Выполнить: `.venv/bin/pytest feeder/tests/test_main.py -v`
Ожидается: `ModuleNotFoundError: No module named 'ace_rh_feeder.main'`

- [ ] **Шаг 3: Реализовать перезапуск адаптера и цикл**

Создать `feeder/ace_rh_feeder/adapter.py`:

```python
"""Восстановление залипшего BLE-адаптера.

RTL8761BU перестаёт отдавать результаты LE-сканирования, оставаясь при этом
включённым и внешне исправным: классическое обнаружение продолжает работать,
в dmesg ничего нет. Лечится перезапуском питания контроллера через BlueZ.
"""
import logging
import subprocess
import time

log = logging.getLogger(__name__)


def reset_adapter(name='hci0', runner=None):
    run = runner or (lambda args: subprocess.run(args, timeout=20).returncode)
    try:
        run(['bluetoothctl', 'power', 'off'])
        if runner is None:
            time.sleep(3)
        run(['bluetoothctl', 'power', 'on'])
        if runner is None:
            time.sleep(3)
        log.warning('адаптер %s перезапущен', name)
        return True
    except Exception as e:
        log.error('не удалось перезапустить адаптер %s: %s', name, e)
        return False
```

Создать `feeder/ace_rh_feeder/main.py`:

```python
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

log = logging.getLogger(__name__)


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
    feeder = Feeder(
        sensor=XiaomiSensor(os.environ['SENSOR_MAC'],
                            adapter=os.environ.get('BLE_ADAPTER', 'hci0')),
        moonraker=Moonraker(os.environ['MOONRAKER_URL']),
        ace_index=int(os.environ.get('ACE_INDEX', '0')),
        ttl=int(os.environ.get('RH_TTL_SECONDS', '1800')))
    log.info('запуск: опрос раз в %d с, срок годности показания %d с',
             interval, feeder.ttl)
    while True:
        await feeder.run_once()
        await asyncio.sleep(interval)


if __name__ == '__main__':
    asyncio.run(main())
```

- [ ] **Шаг 4: Убедиться, что тесты проходят**

Выполнить: `.venv/bin/pytest feeder/tests -v`
Ожидается: 15 passed

- [ ] **Шаг 5: Зафиксировать**

```bash
git add feeder/ace_rh_feeder/adapter.py feeder/ace_rh_feeder/main.py feeder/tests/test_main.py
git commit -m "feat: главный цикл сервиса и восстановление залипшего адаптера"
```

---

### Task 10: Контейнер и проверка всей цепочки

**Файлы:**
- Создать: `feeder/Dockerfile`
- Создать: `feeder/docker-compose.yml`
- Создать: `feeder/requirements.txt`
- Создать: `README.md`

**Интерфейсы:**
- Потребляет: всё из задач 1–9.
- Отдаёт: работающий контейнер `ace-rh-feeder`.

- [ ] **Шаг 1: Создать сборку**

Создать `feeder/requirements.txt`:

```
bleak==0.22.3
```

Создать `feeder/Dockerfile`:

```dockerfile
FROM python:3.12-slim

# bluez нужен для bluetoothctl: им перезапускается залипший адаптер
RUN apt-get update && apt-get install -y --no-install-recommends bluez \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ace_rh_feeder ./ace_rh_feeder

CMD ["python", "-m", "ace_rh_feeder.main"]
```

Создать `feeder/docker-compose.yml`:

```yaml
services:
  ace-rh-feeder:
    build: .
    container_name: ace-rh-feeder
    restart: unless-stopped
    # host-сеть и сокет D-Bus нужны для доступа к BlueZ, как у контейнера HA
    network_mode: host
    volumes:
      - /run/dbus:/run/dbus
    environment:
      - SENSOR_MAC=AA:BB:CC:DD:EE:FF
      - MOONRAKER_URL=http://<printer-ip>:7125
      - ACE_INDEX=0
      - INTERVAL_SECONDS=300
      - RH_TTL_SECONDS=1800
      - BLE_ADAPTER=hci0
      - TZ=UTC
```

- [ ] **Шаг 2: Собрать и запустить**

```bash
cd feeder && docker compose up -d --build && sleep 60 && docker logs ace-rh-feeder
```

Ожидается строка вида
`57.0%rH, 26.95 C, батарея 3033 мВ -> принтер: принято`.

- [ ] **Шаг 3: Убедиться, что принтер получил значение**

```bash
curl -s -X POST http://<printer-ip>:7125/printer/gcode/script \
  -H 'Content-Type: application/json' -d '{"script":"ACE_EXT_RH_STATUS"}'
sleep 2
curl -s "http://<printer-ip>:7125/server/gcode_store?count=10" \
  | python3 -c "import json,sys;[print(m['message']) for m in json.load(sys.stdin)['result']['gcode_store'][-4:]]"
```

Ожидается строка состояния с влажностью, возрастом менее 300 с и пометкой
`годно`.

- [ ] **Шаг 4: Проверить срабатывание автосушки безопасной подделкой**

Включить автосушку с заведомо низким порогом старта и убедиться, что
нагрев включился, затем сразу вернуть нормальные пороги:

```bash
G() { curl -s -X POST http://<printer-ip>:7125/printer/gcode/script \
      -H 'Content-Type: application/json' -d "{\"script\":\"$1\"}"; }
D() { curl -s "http://<printer-ip>:7125/printer/objects/query?ace" \
      | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['status']['ace']['dryer_status'])"; }

G "ACE_EXT_RH_ENABLE ACE=0 ENABLE=1 RH_START=5 RH_END=1 TEMP=45"
G "ACE_SET_RH ACE=0 RH=50 TTL=300"
sleep 70; D
```

Ожидается `{'status': 'drying', 'target_temp': 45, ...}` — сработал
штатный механизм multiACE.

Затем проверить аварийную остановку по протуханию:

```bash
docker stop ace-rh-feeder
sleep 320; D
```

Ожидается `{'status': 'stop', ...}` — показание протухло, нагрев погашен.
Это проверка требования №1 из зоны внимания на живом железе.

Вернуть рабочие настройки и сервис:

```bash
G "ACE_EXT_RH_ENABLE ACE=0 ENABLE=1 RH_START=45 RH_END=35 TEMP=50"
docker start ace-rh-feeder
```

- [ ] **Шаг 5: Написать README**

Создать `README.md`:

```markdown
# Автосушка ACE Pro по внешнему датчику влажности

ACE Pro не имеет датчика влажности, поэтому штатная автосушка multiACE для
него выключена. Внешний BLE-датчик Xiaomi LYWSD03MMC измеряет воздух в
камере, сервер передаёт показания в Klipper, а решения о нагреве принимает
сам multiACE.

## Части

- `printer/ace_ext_rh.py` — модуль Klipper. Принимает влажность командой
  `ACE_SET_RH`, раз в 60 с сверяет её с порогами multiACE и вызывает его
  же механизм автосушки. `ace.py` не изменяется.
- `feeder/` — контейнер на сервере: читает датчик по BLE и раз в 5 минут
  отправляет значение в Moonraker.

## Команды на принтере

    ACE_SET_RH ACE=0 RH=42.5 TTL=1800      подать показание
    ACE_EXT_RH_ENABLE ACE=0 ENABLE=1       включить автосушку
    ACE_EXT_RH_STATUS                      показать состояние

Пороги задаются при включении (`RH_START`, `RH_END`, `TEMP`) и хранятся в
настройках multiACE.

## Безопасность

Нагрев запускается командой с `duration=600` минут — устройство само не
остановится. Поэтому при отсутствии свежих показаний дольше `TTL` модуль
гасит нагрев принудительно: севшая батарейка, упавший сервер или пропавшая
сеть не оставят сушилку включённой. Цикл, запущенный вручную, модуль не
трогает никогда.

## Тесты

    .venv/bin/pytest

## Развёртывание

    ./scripts/deploy_printer.sh                      # модуль на принтер
    curl -X POST http://<printer-ip>:7125/printer/firmware_restart
    cd feeder && docker compose up -d --build        # сервис на сервере
```

- [ ] **Шаг 6: Зафиксировать**

```bash
git add feeder/Dockerfile feeder/docker-compose.yml feeder/requirements.txt README.md
git commit -m "feat: контейнер сервиса и проверка всей цепочки на железе"
```
