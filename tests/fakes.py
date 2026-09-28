"""Подставные объекты Klipper и multiACE для тестов без принтера."""


_SENTINEL = object()


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

    def pause(self, waketime):
        """Klipper отдаёт управление до указанного момента. В тестах время
        просто перескакивает вперёд, иначе цикл ожидания ответа никогда бы
        не закончился."""
        if waketime > self.time:
            self.time = waketime
        return self.time

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

    def get_int(self, name, default=_SENTINEL, minval=None, maxval=None):
        return self._get(name, default, int, minval, maxval)

    def get_float(self, name, default=_SENTINEL, minval=None, maxval=None):
        return self._get(name, default, float, minval, maxval)

    def get(self, name, default=_SENTINEL):
        """Строковый параметр. Klipper отдаёт значение как есть, без
        приведения типа, и поднимает ошибку, если обязательного нет."""
        raw = self.params.get(name)
        if raw is None:
            if default is _SENTINEL:
                raise FakeGcodeError('missing %s' % name)
            return default
        return str(raw)

    def respond_info(self, msg):
        self.responses.append(msg)


class FakeGcodeError(Exception):
    pass


class FakeGcode:
    def __init__(self):
        self.commands = {}
        self.scripts = []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def run_script_from_command(self, script):
        # Модуль двигает филамент только через команды multiACE, своего
        # доступа к моторам у него нет. Здесь запоминаем, что именно он
        # попросил выполнить.
        self.scripts.append(script)

    def error(self, msg):
        return FakeGcodeError(msg)


class FakeAce:
    """Объект multiACE в объёме, который использует ace_ext_rh."""

    def __init__(self, v2=False, printing=False, connected=True):
        self._auto_dry_cfg = {}
        self._auto_dry_started = set()
        self._auto_dry_follow_until = {}
        self._drying = set()
        self._v2 = set([0]) if v2 else set()
        self._printing = printing
        self._connected_per_ace = dict((i, connected) for i in range(4))
        # Слушается ли устройство команды остановки. Настоящий multiACE
        # снимает владение сразу и не проверяет ответ устройства, поэтому
        # неудачная остановка выглядит так: владение снято, а устройство
        # продолжает сушить.
        self.stop_obeyed = True
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
        """Как в multiACE: владение снимается безусловно, а вот прекратил ли
        нагрев само устройство — зависит от того, дошла ли команда."""
        self.stopped_calls.append((idx, why))
        self._auto_dry_started.discard(idx)
        if self.stop_obeyed:
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
        val = int(self._values.get(name, default))
        if minval is not None and val < minval:
            raise ValueError('%s below %s' % (name, minval))
        if maxval is not None and val > maxval:
            raise ValueError('%s above %s' % (name, maxval))
        return val


class AceWithout:
    """Объект multiACE, в котором один атрибут отсутствует — так выглядит
    переименование метода после обновления multiACE."""

    def __init__(self, ace, missing):
        self._ace = ace
        self._missing = missing

    def __getattr__(self, name):
        if name == self._missing:
            raise AttributeError(name)
        return getattr(self._ace, name)


class FakeSaveVariables:
    """Объект [save_variables] Klipper в нужном объёме: модуль читает
    хранилище через allVariables, а пишет через ace.save_variable — поэтому
    словарь здесь тот же самый, что и у подставного multiACE."""

    def __init__(self, store):
        self.allVariables = store


class FakePrinter:
    def __init__(self, ace=None):
        self.reactor = FakeReactor()
        self.objects = {'gcode': FakeGcode()}
        if ace is not None:
            self.objects['ace'] = ace
            store = getattr(ace, 'saved', None)
            if store is not None:
                self.objects['save_variables'] = FakeSaveVariables(store)
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
