# Инструмент разведки протокола ACE.
#
# Две задачи, обе нужны для работы над прошивкой:
#
# 1. Отправить в ACE произвольный метод и показать ответ. Слой протокола
#    первой ревизии (ace_protocol_v1.py) пропускает любое имя метода как
#    JSON, поэтому так можно проверить, нет ли в заводской прошивке
#    недокументированных команд — например, туннеля к считывателю
#    (filament_identify) или команды обновления прошивки.
#
# 2. Точно измерить длину кадра ответа get_status. Это не праздный вопрос:
#    кадр длиннее 1024 байт вешает ACE наглухо, а патч прошивки собирается
#    добавить в каждый слот поле uid. Знать фактический запас надо ДО
#    того, как патч написан.
#
# Файл ace.py не изменяется: модуль обращается к объекту ace снаружи и при
# несовместимости громко отключается, как и ace_ext_rh.py.
import json
import logging

REQUIRED_ACE_ATTRS = ('send_request_to', 'reactor', '_protocols')

# Предел кадра, за которым ACE зависает. Значение из разбора протокола
# сообществом, подтверждать его экспериментально категорически не стоит.
FRAME_LIMIT = 1024

# Начало кадра и размер заголовка: 0xFF 0xAA, затем длина полезной
# нагрузки двумя байтами little-endian.
FRAME_HEADER = b'\xff\xaa'
FRAME_HEADER_LEN = 4

# Методы, которые заведомо ничего не двигают и не греют. Всё остальное
# требует явного FORCE=1: неизвестный метод может оказаться командой
# подачи, и филамент поедет в неожиданный момент.
SAFE_METHODS = (
    'get_info',
    'get_status',
    'get_filament_info',
    'get_feed_info',
)

REQUEST_TIMEOUT = 3.0

# Команда принудительного чтения метки по слоту. В публичном описании
# протокола её нет, она найдена разбором прошивки V1.3.863. Филамент она не
# двигает: в исходной оснастке подача вызывалась отдельной командой.
RECOGNITION_METHOD = 'filament_recognition'
FILAMENT_INFO_METHOD = 'get_filament_info'

# Отказ устройства выполнить чтение: слот занят активной подачей. Это не
# «антенна не видит», это «сейчас нельзя», и смешивать их нельзя.
BUSY_MSG = 'FORBIDDEN'

# Сколько ждать между командой чтения и запросом результата. Устройство
# отвечает на саму команду сразу, а само чтение идёт асинхронно, и поле
# заполняется позже. Замер на живом устройстве: после принятой команды оно
# считает себя занятым ещё около пяти-девяти секунд.
RECOGNITION_WAIT = 6.0

# Пауза перед повтором после отказа «занято» и число таких повторов внутри
# одного раунда. Без паузы тест бил в окно занятости без остановки и сам же
# получал сплошные отказы: проверено, двадцать раундов укладывались в три
# секунды и давали ноль ответов там, где при выдержке чтение проходит.
BUSY_BACKOFF = 3.0
BUSY_RETRIES = 4

# --- туннель к считывателю ------------------------------------------------
#
# Патч прошивки (ACE_V1.3.863_tunnel4.bin) добавляет в filament_recognition
# ветку: значение index >= 0x20000 — это уже не номер слота, а команда
# считывателю. Раскладка ровно та, которую собирает стаб:
#
#   бит 17      — магия, всегда 1
#   биты 16:15  — считыватель 0..3
#   бит 14      — 1 запись, 0 чтение
#   биты 13:8   — регистр MFRC522 0..0x3F
#   биты 7:0    — данные при записи, смещение в буфере при чтении
TUNNEL_MAGIC_BIT = 17

# Регистры, которыми патч расширяет обычные чтения и записи.
TUNNEL_STATUS_REG = 0x3D   # байт состояния 0x20000127
TUNNEL_BUFFER_REG = 0x3E   # сырой буфер антиколлизии 0x200067D0

# Сколько раундов опознания делать и сколько одинаковых подряд считать
# доказательством. Одиночному чтению верить нельзя: когда в поле одной
# антенны оказываются две метки (а на этом железе один чип обслуживает
# ПАРУ слотов), штатная антиколлизия возвращает то одну, то другую, то
# ничего. Ровно так устроена проверка в multiACE (_rc_stable_uid).
UID_ROUNDS = 4

# Поиск метки доворотом катушки. Метка сидит на катушке, антенна одна на
# пару слотов и стоит между ними, поэтому читается метка только в своём
# секторе поворота. Подача проворачивает катушку и переводит метку в
# сектор — это единственный способ повлиять на чтение снаружи.
#
# Масштаб задаётся длиной окружности катушки, а не осторожностью: чтобы
# провести метку мимо антенны, нужен оборот. При намотке на радиусе
# 50–90 мм полный оборот — это 300–560 мм филамента, поэтому шаг в
# сантиметры не двигает метку заметно вовсе. Владелец указал рабочий
# масштаб прямо: протягивать надо порядка 40 см.
SEEK_STEP = 100
SEEK_NUDGE = 100
SEEK_ROUNDS = 8
SEEK_SPEED = 20

# Сколько филамента разрешено смотать на КАЖДУЮ катушку при подтверждении
# принадлежности. Сектор чтения оказался шире одного доворота: на живом
# устройстве метка оставалась в поле и после 25 мм. Поэтому доворачиваем
# нарастающе, но с потолком — крутить бесконечно нельзя.
VERIFY_BUDGET = 800


def pack_tunnel(reader, write, reg, data=0):
    """Собрать значение index для туннеля.

    Границы проверяются здесь, и это не формальность: таблица распиновки в
    прошивке (0x0800AC44) индексируется номером считывателя без единого
    сравнения, поэтому значение вне 0..3 уводит чтение в чужую память."""
    if not 0 <= reader <= 3:
        raise ValueError(
            'считыватель должен быть 0..3, получено %r' % (reader,))
    if write not in (0, 1):
        raise ValueError('признак записи 0 или 1, получено %r' % (write,))
    if not 0 <= reg <= 0x3F:
        raise ValueError('регистр должен быть 0..0x3F, получено %r' % (reg,))
    if not 0 <= data <= 0xFF:
        raise ValueError('данные должны быть 0..255, получено %r' % (data,))
    return ((1 << TUNNEL_MAGIC_BIT) | (reader << 15) | (write << 14)
            | (reg << 8) | data)


def tunnel_value(response):
    """Достать значение туннеля из ответа или None, если ответ не от него.

    Две формы: короткая `{"id":N,"v":N}` у текущего стаба и вложенная
    `{"result":{"v":N}}` у первой сборки. Заводская прошивка поля v не
    отдаёт вовсе — по этому None и отличает «нет метки» от «нет туннеля»."""
    if not isinstance(response, dict):
        return None
    if isinstance(response.get('v'), int):
        return response['v']
    result = response.get('result')
    if isinstance(result, dict) and isinstance(result.get('v'), int):
        return result['v']
    return None


def word_bytes(word):
    """Четыре байта слова, старшим вперёд.

    Порядок именно такой, потому что стаб собирает слово сдвигами
    `buf[0] << 24 | buf[1] << 16 | buf[2] << 8 | buf[3]`."""
    return bytes(((word >> 24) & 0xFF, (word >> 16) & 0xFF,
                  (word >> 8) & 0xFF, word & 0xFF))


def uid_from_buffer(buf):
    """UID из сырого буфера антиколлизии или None, если буфер не сходится.

    Раскладка ISO14443A для метки двойного размера:
      [0] 0x88 — каскадный тег, [1..3] UID0..2, [4] BCC0,
      [5..8] UID3..6, [9] BCC1.
    Контрольные суммы несут здесь всю нагрузку по доверию: именно BCC0
    отличает настоящий байт 0xEF от прежнего ошибочного 0xE5."""
    if len(buf) < 10:
        return None
    if not any(buf[:10]):
        return None
    if buf[0] != 0x88:
        return None
    if buf[4] != (buf[0] ^ buf[1] ^ buf[2] ^ buf[3]):
        return None
    if buf[9] != (buf[5] ^ buf[6] ^ buf[7] ^ buf[8]):
        return None
    level1 = bytes(buf[1:4])
    level2 = bytes(buf[5:9])
    # Целиком нулевой уровень каскада — признак оборванного чтения, а не
    # метки. Контрольный байт для нулей тоже ноль, поэтому такой кадр
    # формально сходится и без этой проверки был бы принят. Снято с
    # живого устройства: 53427000000000 вместо 534270D1B50001, метка на
    # краю поля. Отдельные нулевые байты внутри уровня допустимы.
    if not any(level1) or not any(level2):
        return None
    return (level1 + level2).hex().upper()


class AceExtRaw:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.ace = None
        self.disabled_reason = None
        self._armed = {}
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('ACE_EXT_RAW', self.cmd_ACE_EXT_RAW,
                               desc=self.cmd_ACE_EXT_RAW_help)
        gcode.register_command('ACE_EXT_FRAME_LEN', self.cmd_ACE_EXT_FRAME_LEN,
                               desc=self.cmd_ACE_EXT_FRAME_LEN_help)
        gcode.register_command('ACE_EXT_ANTENNA_TEST',
                               self.cmd_ACE_EXT_ANTENNA_TEST,
                               desc=self.cmd_ACE_EXT_ANTENNA_TEST_help)
        gcode.register_command('ACE_EXT_FW_RELEASE',
                               self.cmd_ACE_EXT_FW_RELEASE,
                               desc=self.cmd_ACE_EXT_FW_RELEASE_help)
        gcode.register_command('ACE_EXT_FW_RESUME',
                               self.cmd_ACE_EXT_FW_RESUME,
                               desc=self.cmd_ACE_EXT_FW_RESUME_help)
        gcode.register_command('ACE_EXT_UID_READ', self.cmd_ACE_EXT_UID_READ,
                               desc=self.cmd_ACE_EXT_UID_READ_help)
        gcode.register_command('ACE_EXT_TAG_SEEK', self.cmd_ACE_EXT_TAG_SEEK,
                               desc=self.cmd_ACE_EXT_TAG_SEEK_help)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

    def _handle_ready(self):
        ace = self.printer.lookup_object('ace', None)
        reason = self._check_compatibility(ace)
        if reason is not None:
            self.ace = None
            self.disabled_reason = reason
            logging.error('[ace_ext_raw] ОТКЛЮЧЁН: %s' % reason)
            return
        self.ace = ace
        self.disabled_reason = None
        logging.info('[ace_ext_raw] готов')

    def _check_compatibility(self, ace):
        if ace is None:
            return 'объект multiACE не найден'
        missing = [a for a in REQUIRED_ACE_ATTRS if not hasattr(ace, a)]
        if missing:
            return ('multiACE несовместим, отсутствует: %s'
                    % ', '.join(missing))
        return None

    def _require_ace(self, gcmd):
        if self.ace is None:
            raise self.printer.lookup_object('gcode').error(
                '[ace_ext_raw] отключён: %s' % self.disabled_reason)
        return self.ace

    # --- произвольный метод -------------------------------------------------

    cmd_ACE_EXT_RAW_help = (
        '[ace_ext_raw] Отправить в ACE произвольный метод и показать ответ: '
        'ACE_EXT_RAW METHOD=get_info [ACE=0] [INDEX=n] [PARAMS={"a":1}] '
        '[FORCE=1]. Методы вне списка безопасных требуют FORCE=1: '
        'неизвестная команда может оказаться подачей филамента.')

    def cmd_ACE_EXT_RAW(self, gcmd):
        ace = self._require_ace(gcmd)
        gcode = self.printer.lookup_object('gcode')
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        method = gcmd.get('METHOD')
        force = gcmd.get_int('FORCE', 0, minval=0, maxval=1)

        if method not in SAFE_METHODS and not force:
            raise gcode.error(
                '[ace_ext_raw] метод %r не в списке безопасных. Он может '
                'двигать филамент или включать нагрев. Если это осознанное '
                'решение и принтер простаивает, добавьте FORCE=1. '
                'Безопасные: %s' % (method, ', '.join(SAFE_METHODS)))

        params = {}
        raw = gcmd.get('PARAMS', None)
        if raw is not None:
            params = self._parse_params(gcode, raw)
        index = gcmd.get_int('INDEX', None)
        if index is not None:
            params['index'] = index

        response = self._request(ace, idx, method, params)
        if response is None:
            gcmd.respond_info(
                '[ace_ext_raw] ответа нет за %.1f с. Для заводской прошивки '
                'это обычно значит, что метод %r ей неизвестен.'
                % (REQUEST_TIMEOUT, method))
            return
        gcmd.respond_info('[ace_ext_raw] %s -> %s'
                          % (method, json.dumps(response, ensure_ascii=False)))

    @staticmethod
    def _parse_params(gcode, raw):
        """Разобрать PARAMS в словарь.

        Две формы. JSON для сложных случаев и простая `имя=значение` через
        запятую — без кавычек и пробелов. Простая нужна потому, что до
        модуля кавычки доезжают не всегда: строка проходит разбор G-code, и
        JSON вида {"type":1} приходил уже без них. Числа приводятся к int,
        остальное остаётся строкой."""
        raw = raw.strip()
        if raw.startswith('{'):
            try:
                out = json.loads(raw)
            except ValueError as e:
                raise gcode.error(
                    '[ace_ext_raw] PARAMS не разбирается как JSON: %s. '
                    'Проще написать без кавычек: PARAMS=type=1' % e)
            if not isinstance(out, dict):
                raise gcode.error('[ace_ext_raw] PARAMS должен быть объектом')
            return out
        out = {}
        for pair in raw.split(','):
            if not pair:
                continue
            if '=' not in pair:
                raise gcode.error(
                    '[ace_ext_raw] PARAMS: ожидается имя=значение через '
                    'запятую, получено %r' % pair)
            k, v = pair.split('=', 1)
            k = k.strip()
            v = v.strip()
            try:
                out[k] = int(v, 0)
            except ValueError:
                out[k] = v
        return out

    def _request(self, ace, idx, method, params):
        """Синхронный запрос из гринлета: послать и дождаться ответа.

        Обратный вызов диспетчера multiACE зовётся с ИМЕНОВАННЫМИ
        аргументами (self=<ace>, response=<ret>), поэтому подпись в форме
        **kw — единственная, которая переживает оба стиля вызова. Позиционная
        подпись с другими именами приводит к TypeError внутри реактора, а это
        полная остановка Klipper."""
        box = {}

        def _cb(*args, **kw):
            box['r'] = kw.get('response', args[-1] if args else None)

        try:
            ace.send_request_to(idx, {'method': method,
                                      'params': dict(params or {})}, _cb)
        except Exception as e:
            logging.info('[ace_ext_raw] отправка %s не удалась: %s'
                         % (method, e))
            return None
        deadline = self.reactor.monotonic() + REQUEST_TIMEOUT
        while 'r' not in box and self.reactor.monotonic() < deadline:
            self.reactor.pause(self.reactor.monotonic() + 0.005)
        return box.get('r')

    # --- измерение длины кадра ---------------------------------------------

    cmd_ACE_EXT_FRAME_LEN_help = (
        '[ace_ext_raw] Измерить фактическую длину кадров от ACE: '
        'ACE_EXT_FRAME_LEN ARM=1 включает замер, ARM=0 выключает и '
        'показывает результат. Нужно, чтобы знать запас до предела в '
        '1024 байта перед добавлением поля uid в прошивку.')

    def cmd_ACE_EXT_FRAME_LEN(self, gcmd):
        ace = self._require_ace(gcmd)
        gcode = self.printer.lookup_object('gcode')
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        arm = gcmd.get_int('ARM', 1, minval=0, maxval=1)

        protocol = ace._protocols.get(idx)
        if protocol is None:
            raise gcode.error('[ace_ext_raw] у ACE %d нет протокола'
                              % (idx + 1))

        if arm:
            if idx in self._armed:
                raise gcode.error('[ace_ext_raw] замер на ACE %d уже идёт'
                                  % (idx + 1))
            self._arm(protocol, idx)
            gcmd.respond_info(
                '[ace_ext_raw] замер включён для ACE %d. Дайте пройти '
                'нескольким опросам состояния, затем ACE_EXT_FRAME_LEN '
                'ARM=0.' % (idx + 1))
            return

        state = self._armed.pop(idx, None)
        if state is None:
            raise gcode.error('[ace_ext_raw] замер на ACE %d не включался'
                              % (idx + 1))
        protocol.decode_frames = state['original']
        if not state['max']:
            gcmd.respond_info('[ace_ext_raw] кадров не поймано')
            return
        gcmd.respond_info(
            '[ace_ext_raw] ACE %d: кадров %d, самый длинный %d байт, '
            'запас до предела %d байт (предел %d)'
            % (idx + 1, state['count'], state['max'],
               FRAME_LIMIT - state['max'], FRAME_LIMIT))

    def _arm(self, protocol, idx):
        """Обернуть разбор кадров и запоминать длины из заголовков.

        Длина берётся из самого кадра, а не из повторной сериализации
        разобранного словаря: словарь теряет поля, которых нет в модели
        multiACE, и оценка вышла бы заниженной — ровно в ту сторону, в
        которую ошибаться опаснее всего."""
        original = protocol.decode_frames
        state = {'original': original, 'max': 0, 'count': 0}

        def wrapper(buffer):
            try:
                state['count'] += self._scan_lengths(buffer, state)
            except Exception:
                logging.exception('[ace_ext_raw] замер длины кадра')
            return original(buffer)

        protocol.decode_frames = wrapper
        self._armed[idx] = state

    @staticmethod
    def _scan_lengths(buffer, state):
        data = bytes(buffer)
        found = 0
        pos = 0
        while True:
            pos = data.find(FRAME_HEADER, pos)
            if pos < 0 or pos + FRAME_HEADER_LEN > len(data):
                break
            length = data[pos + 2] | (data[pos + 3] << 8)
            if 0 < length <= FRAME_LIMIT * 4:
                found += 1
                if length > state['max']:
                    state['max'] = length
            pos += len(FRAME_HEADER)
        return found


    # --- тест антенн --------------------------------------------------------

    cmd_ACE_EXT_ANTENNA_TEST_help = (
        '[ace_ext_raw] Проверить антенны: ACE_EXT_ANTENNA_TEST [ACE=0] '
        '[SLOT=n] [ROUNDS=3]. Принудительно запускает чтение метки в слоте и '
        'смотрит, ответила ли она. Без SLOT проверяются все четыре слота. '
        'Филамент не двигается.')

    def cmd_ACE_EXT_ANTENNA_TEST(self, gcmd):
        ace = self._require_ace(gcmd)
        gcode = self.printer.lookup_object('gcode')
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        rounds = gcmd.get_int('ROUNDS', 3, minval=1, maxval=20)
        one = gcmd.get_int('SLOT', None, minval=0, maxval=3)
        slots = [one] if one is not None else [0, 1, 2, 3]

        if self._is_printing():
            raise gcode.error(
                '[ace_ext_raw] тест антенн только на простое: во время печати '
                'принудительное чтение соперничает с собственными опросами '
                'устройства')

        gcmd.respond_info('[ace_ext_raw] тест антенн, раундов %d' % rounds)
        seen = {}
        for slot in slots:
            r = self._probe_slot(ace, idx, slot, rounds)
            gcmd.respond_info(
                '[ace_ext_raw] плата %s, слот %d: ответов %d из %d%s%s'
                % (self._reader_name(slot), slot + 1, r['answered'],
                   r['rounds'],
                   (', отказов «занято» %d' % r['busy']) if r['busy'] else '',
                   self._describe(r)))
            for v in r['values']:
                seen.setdefault(v, []).append(slot + 1)
        for value, owners in seen.items():
            if len(set(owners)) > 1:
                gcmd.respond_info(
                    '[ace_ext_raw] ВНИМАНИЕ: значение %s пришло сразу из '
                    'слотов %s. Устройство отдаёт последнее удачное чтение '
                    'всем слотам, у которых своего нет, поэтому привязывать '
                    'катушку по такому значению НЕЛЬЗЯ — расход спишется не '
                    'с той.' % (value, ', '.join(str(o) for o in sorted(set(owners)))))

    def _probe_slot(self, ace, idx, slot, rounds):
        """Один слот: принудительное чтение, затем запрос результата У САМОГО
        УСТРОЙСТВА.

        Снимок состояния multiACE для этого не годится: он обновляется своим
        опросом, и слот, у которого артикул заполнен с прошлого раза,
        выглядел бы успешно прочитанным, даже если сейчас антенна не
        ответила вовсе. Проверено на живом устройстве: слот с активной
        подачей отвечает на команду чтения отказом, а артикул в снимке
        остаётся прежним."""
        answered = 0
        busy = 0
        values = []
        for _ in range(rounds):
            reply = self._recognise_with_backoff(ace, idx, slot)
            if reply is None:
                busy += 1
                continue
            self.reactor.pause(self.reactor.monotonic() + RECOGNITION_WAIT)
            info = self._request(ace, idx, FILAMENT_INFO_METHOD,
                                 {'index': slot})
            sku = ''
            if isinstance(info, dict):
                result = info.get('result')
                if isinstance(result, dict):
                    sku = str(result.get('sku') or '')
            if sku:
                answered += 1
                values.append(sku)
        return {'answered': answered, 'busy': busy, 'rounds': rounds,
                'values': values}

    def _recognise_with_backoff(self, ace, idx, slot):
        """Добиться принятой команды чтения, пережидая занятость.

        Устройство принимает чтение, а следующие несколько секунд отвечает
        отказом «занято». Считать такой отказ неудачей антенны нельзя: это
        разные вещи. Поэтому отказ не сжигает раунд, а приводит к выдержке и
        повтору. Возвращает ответ устройства или None, если занятость так и
        не отпустила."""
        for attempt in range(BUSY_RETRIES + 1):
            if attempt:
                self.reactor.pause(self.reactor.monotonic() + BUSY_BACKOFF)
            reply = self._request(ace, idx, RECOGNITION_METHOD,
                                  {'index': slot})
            if reply is None:
                continue
            if str(reply.get('msg', '')).strip().upper() != BUSY_MSG:
                return reply
        return None

    @staticmethod
    def _reader_name(slot):
        """Слоты сидят парами на одной плате считывателя: 1-2 на первой,
        3-4 на второй. То же соответствие, что у multiACE (slot >> 1)."""
        return 'NFC%d' % ((slot >> 1) + 1)

    @staticmethod
    def _describe(r):
        values = sorted(set(r['values']))
        if values:
            return ', прочитано: %s' % ', '.join(values)
        if r['busy'] == r['rounds']:
            return (', устройство ни разу не приняло команду чтения даже с '
                    'выдержкой — слот занят подачей, это не отказ антенны')
        return (', метка ни разу не ответила. Причин две и по этим данным '
                'они неразличимы: метки нет вовсе, либо она сейчас не '
                'напротив антенны (метка сидит на одном месте катушки)')

    def _is_printing(self):
        stats = self.printer.lookup_object('print_stats', None)
        state = (getattr(stats, 'state', '') or '').lower()
        return state in ('printing', 'paused')


    # --- освобождение порта для прошивки --------------------------------

    # Что нужно объекту multiACE именно для этих двух команд. Проверяется
    # на месте, а не при загрузке: отсутствие этих атрибутов не повод
    # лишать пользователя разведки протокола и теста антенн.
    FW_ATTRS = ('_fw_update_hold', '_disconnect_from', '_open_ace',
                '_ace_devices', '_connected_per_ace')

    # Сколько держать защитный флаг переподключения после возврата порта.
    # За это время устройство успевает загрузиться после прошивки и
    # прислать первый настоящий отчёт о состоянии слотов.
    RESUME_GUARD_S = 12.0

    cmd_ACE_EXT_FW_RELEASE_help = (
        '[ace_ext_raw] Отпустить последовательный порт ACE для прошивки: '
        'ACE_EXT_FW_RELEASE ACE=0. Работает на ACE Pro первой ревизии, в '
        'отличие от штатной ACE_FW_RELEASE. Вернуть порт: ACE_EXT_FW_RESUME.')

    def cmd_ACE_EXT_FW_RELEASE(self, gcmd):
        """Отпустить порт, не останавливая Klipper.

        Штатная ACE_FW_RELEASE отказывает первой ревизии, но отказ стоит
        ТОЛЬКО в обработчике команды: сам механизм удержания к протоколу
        безразличен. `_fw_update_hold` — обычное множество, а единственная
        точка, где оно проверяется, это `_open_ace`, и там никакой проверки
        на ревизию нет. Поэтому достаточно поставить удержание и закрыть
        порт своими руками.

        Зачем: без этого прошивка требует остановки службы Klipper, а её
        ветвь остановки снимает питание с микроконтроллеров, то есть
        обнуляет записи слотов ACE и привязки катушек. Здесь ничего этого
        не происходит.

        Те же предохранители, что у штатной команды: не во время печати и
        не во время смены филамента. Плюс гасим автосушку: оставлять
        нагреватель включённым, когда управлять им больше нечем, нельзя."""
        ace = self._require_ace(gcmd)
        gcode = self.printer.lookup_object('gcode')
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        self._require_fw_support(gcmd, ace)

        if idx >= len(ace._ace_devices):
            raise gcode.error('[ace_ext_raw] ACE %d не существует' % (idx + 1))
        if self._is_printing() or (
                getattr(ace, '_is_actively_printing', lambda: False)()):
            raise gcode.error(
                '[ace_ext_raw] идёт печать — отпускать порт нельзя')
        if getattr(ace, '_swap_in_progress', False):
            raise gcode.error(
                '[ace_ext_raw] идёт смена филамента — отпускать порт нельзя')

        if idx in getattr(ace, '_auto_dry_started', ()):
            try:
                ace._auto_dry_stop(idx, 'прошивка')
            except Exception as e:
                logging.warning('[ace_ext_raw] не удалось погасить автосушку '
                                'ACE %d: %s' % (idx, e))

        ace._fw_update_hold.add(idx)
        if ace._connected_per_ace.get(idx, False):
            ace._disconnect_from(idx)

        gcmd.respond_info(
            '[ace_ext_raw] ACE %d отпущен, порт %s свободен. ВНИМАНИЕ: пока '
            'порт отпущен, Klipper перезапускать НЕЛЬЗЯ — удержание живёт '
            'только в памяти, и перезапуск подключит устройство посреди '
            'прошивки. Вернуть: ACE_EXT_FW_RESUME ACE=%d'
            % (idx + 1, ace._ace_devices[idx], idx))

    cmd_ACE_EXT_FW_RESUME_help = (
        '[ace_ext_raw] Вернуть порт ACE после прошивки: '
        'ACE_EXT_FW_RESUME ACE=0')

    def cmd_ACE_EXT_FW_RESUME(self, gcmd):
        ace = self._require_ace(gcmd)
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        self._require_fw_support(gcmd, ace)
        before = dict(getattr(ace, '_spool_binding', {}) or {})
        ace._fw_update_hold.discard(idx)

        # Защита привязок катушек на время подъёма связи.
        #
        # multiACE освобождает привязку слота, как только видит его пустым,
        # и гасит это поведение флагом _reconnecting_per_ace. Но флаг
        # ставится ТОЛЬКО в аварийном восстановлении связи, а при ручном
        # открытии порта — нет. После прошивки устройство перезагружается и
        # первый его отчёт вполне может прийти с пустыми слотами, пока
        # датчики не опрошены. Без флага это стоило бы всех привязок, а
        # восстанавливать их пришлось бы руками.
        guard = getattr(ace, '_reconnecting_per_ace', None)
        if guard is not None:
            guard[idx] = True
        ok = False
        try:
            ok = bool(ace._open_ace(idx))
            if guard is not None:
                self.reactor.pause(self.reactor.monotonic()
                                   + self.RESUME_GUARD_S)
        except Exception as e:
            logging.exception('[ace_ext_raw] подключение ACE %d: %s' % (idx, e))
        finally:
            if guard is not None:
                guard[idx] = False

        after = dict(getattr(ace, '_spool_binding', {}) or {})
        lost = [k for k in before if k not in after]
        gcmd.respond_info(
            '[ace_ext_raw] ACE %d: удержание снято, подключение %s, '
            'привязок было %d, стало %d%s'
            % (idx + 1, 'выполнено' if ok else 'НЕ УДАЛОСЬ, см. журнал',
               len(before), len(after),
               (', ПОТЕРЯНЫ: %s' % ', '.join(sorted(lost))) if lost else ''))

    # --- чтение UID через туннель -------------------------------------------

    cmd_ACE_EXT_UID_READ_help = (
        '[ace_ext_raw] Прочитать UID метки в считывателе: '
        'ACE_EXT_UID_READ [ACE=0] READER=0. Нужна прошивка с туннелем; на '
        'заводской сборке команда честно скажет, что туннеля нет. '
        'Филамент не двигается, поля слотов не трогаются.')

    def cmd_ACE_EXT_UID_READ(self, gcmd):
        ace = self._require_ace(gcmd)
        gcode = self.printer.lookup_object('gcode')
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        reader = gcmd.get_int('READER', 0)
        if not 0 <= reader <= 3:
            raise gcode.error(
                '[ace_ext_raw] READER должен быть 0..3, получено %d' % reader)

        seen = []
        for _ in range(UID_ROUNDS):
            uid, buf, failure = self._read_uid_once(ace, idx, reader)
            if failure is not None:
                gcmd.respond_info(failure)
                return
            # Два одинаковых подряд — принимаем. Не «два одинаковых
            # где-нибудь в серии»: чередование двух меток дало бы
            # совпадение и при полной неустойчивости чтения.
            if uid is not None and seen and seen[-1] == uid:
                gcmd.respond_info(
                    '[ace_ext_raw] UID считывателя %d: %s (состояние %d)'
                    % (reader, uid, buf[-1]))
                return
            seen.append(uid)

        distinct = sorted(set(u for u in seen if u))
        if not distinct:
            gcmd.respond_info(
                '[ace_ext_raw] нет метки: считыватель %d за %d попыток не '
                'вернул ни одного целого кадра' % (reader, UID_ROUNDS))
            return
        if len(distinct) == 1:
            # Одна метка, но поймана не дважды подряд. Это край поля, а не
            # столкновение: лечится доворотом катушки подачей, и путать
            # это с двумя метками нельзя — лечение разное.
            gcmd.respond_info(
                '[ace_ext_raw] чтение неустойчиво: считыватель %d ловит '
                'метку %s через раз (%d попыток). Метка на краю поля — '
                'доверните катушку подачей и повторите.'
                % (reader, distinct[0], UID_ROUNDS))
            return
        gcmd.respond_info(
            '[ace_ext_raw] чтение неустойчиво: считыватель %d за %d попыток '
            'отдал разные метки (%s). Скорее всего, в поле антенны две '
            'метки — на этом железе один считыватель обслуживает пару '
            'слотов. Доверять такому чтению нельзя.'
            % (reader, UID_ROUNDS, ', '.join(distinct)))

    def _read_uid_once(self, ace, idx, reader):
        """Один заход: опознать метку и снять буфер с состоянием.

        Возвращает тройку (uid, буфер, причина отказа). Причина не None —
        разговаривать с устройством дальше бессмысленно."""
        # Порядок шагов задан патчом: опознание отдаёт первые четыре байта
        # буфера, два чтения — хвост, последний запрос — байт состояния.
        # Значения ниже 0x20000 не отправляются никогда: такой вызов ушёл бы
        # в штатное распознавание и перезаписал поля слота.
        steps = ((TUNNEL_BUFFER_REG, 0, 1),
                 (TUNNEL_BUFFER_REG, 4, 0),
                 (TUNNEL_BUFFER_REG, 8, 0),
                 (TUNNEL_STATUS_REG, 0, 0))
        words = []
        for reg, data, write in steps:
            packed = pack_tunnel(reader, write, reg, data)
            reply = self._request(ace, idx, RECOGNITION_METHOD,
                                  {'index': packed})
            value = tunnel_value(reply)
            if value is None:
                # Молчание устройства и ответ без поля v — разные беды, и
                # смешивать их нельзя. Ровно на такой подмене смыслов у нас
                # уже обжёгся флаг rfid, где «не вижу метку» и «не разобрал
                # метку» имели одно значение.
                if reply is None:
                    return None, b'', (
                        '[ace_ext_raw] ACE не ответил на запрос к '
                        'считывателю %d. Это не «нет туннеля»: связь с '
                        'устройством или занятость, проверьте связь и '
                        'повторите.' % reader)
                return None, b'', (
                    '[ace_ext_raw] ответа с полем v нет: в прошивке нет '
                    'туннеля к считывателю (заводская сборка). Прочитать '
                    'UID этой командой нечем.')
            words.append(value)

        buf = b''.join(word_bytes(w) for w in words)
        return uid_from_buffer(buf), buf, None

    # --- поиск метки доворотом катушки --------------------------------------

    cmd_ACE_EXT_TAG_SEEK_help = (
        '[ace_ext_raw] Найти метку слота, доворачивая катушку: '
        'ACE_EXT_TAG_SEEK SLOT=0 [ACE=0] [STEP=8] [NUDGE=25] [ROUNDS=6] '
        '[BIND=1]. Антенна одна на пару слотов и стоит между ними, поэтому '
        'метка читается только в своём секторе поворота, а метка соседа '
        'может загораживать эфир. Команда доворачивает катушки подачей, '
        'пока чтение не станет устойчивым.')

    def cmd_ACE_EXT_TAG_SEEK(self, gcmd):
        ace = self._require_ace(gcmd)
        gcode = self.printer.lookup_object('gcode')
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        slot = gcmd.get_int('SLOT', minval=0, maxval=3)
        step = gcmd.get_int('STEP', SEEK_STEP, minval=1, maxval=200)
        nudge = gcmd.get_int('NUDGE', SEEK_NUDGE, minval=1, maxval=200)
        rounds = gcmd.get_int('ROUNDS', SEEK_ROUNDS, minval=1, maxval=20)
        do_bind = gcmd.get_int('BIND', 1)
        verify = gcmd.get_int('VERIFY', 1)
        verify_max = gcmd.get_int('VERIFY_MAX', VERIFY_BUDGET,
                                  minval=0, maxval=2000)
        restore = gcmd.get_int('RESTORE', 1)

        if self._is_printing():
            raise gcode.error(
                '[ace_ext_raw] доворот катушки только на простое: во время '
                'печати подача занята')
        if not self._slot_occupied(ace, idx, slot):
            raise gcode.error(
                '[ace_ext_raw] в слоте %d нет филамента, доворачивать нечего'
                % (slot + 1))

        reader = slot >> 1
        neighbour = slot ^ 1
        self._turned = {}
        try:
            # счёту: команда обязана оставить устройство в том положении, в
            # котором его взяла, иначе следующий поиск начнёт с испорченной
            # позиции — это найдено на живом устройстве.
            self._turned = {}
            moved = self._turned

            self._gate_lost = None
            for attempt in range(rounds + 1):
                uid, distinct = self._stable_uid(ace, idx, reader)
                if uid is not None:
                    break
                # Две разные метки в поле — мешает сосед, и доворачивать надо
                # ЕГО, а не свою катушку: своя, возможно, уже стоит правильно.
                # Пусто или ловится через раз — своя метка вне сектора.
                if len(distinct) > 1 and self._slot_occupied(ace, idx, neighbour):
                    target, length = neighbour, nudge
                else:
                    target, length = slot, step
                if attempt == rounds:
                    break
                if not self._rotate_guarded(gcode, ace, idx, target,
                                            length):
                    self._gate_lost = target
                    break

            report = ('своя %d мм, соседняя %d мм'
                      % (moved.get(slot, 0), moved.get(neighbour, 0)))
            if self._gate_lost is not None:
                gcmd.respond_info(
                    '[ace_ext_raw] остановился: гейт слота %d перестал '
                    'видеть филамент, дальше мотать нельзя — пруток выйдет '
                    'из подачи. Катушки возвращаю на место.'
                    % (self._gate_lost + 1))
            if uid is None:
                gcmd.respond_info(
                    '[ace_ext_raw] метка слота %d не найдена за %d доворотов '
                    '(%s). Видели: %s'
                    % (slot + 1, rounds, report,
                       ', '.join(distinct) if distinct else 'ничего'))
                return

            gcmd.respond_info(
                '[ace_ext_raw] метка слота %d: %s (доворот: %s)'
                % (slot + 1, uid, report))

            if verify:
                verdict = self._whose_tag(gcode, ace, idx, slot, reader, uid,
                                          nudge, verify_max)
                if verdict == 'empty_neighbour':
                    gcmd.respond_info(
                        '[ace_ext_raw] соседний слот %d пуст, чужой метке '
                        'взяться неоткуда — принадлежность несомненна'
                        % (neighbour + 1))
                elif verdict == 'ours':
                    gcmd.respond_info(
                        '[ace_ext_raw] принадлежность подтверждена движением: '
                        'метка ушла из поля при провороте своей катушки')
                elif verdict == 'neighbour':
                    gcmd.respond_info(
                        '[ace_ext_raw] это метка СОСЕДНЕГО слота %d: она не '
                        'реагирует на проворот нашей катушки и уходит при '
                        'провороте соседней. Привязка отменена — иначе расход '
                        'списывался бы с чужой катушки.' % (neighbour + 1))
                    return
                else:
                    gcmd.respond_info(
                        '[ace_ext_raw] не удалось подтвердить принадлежность: '
                        'метка остаётся в поле при провороте обеих катушек. '
                        'Привязка отменена, решать должен человек.')
                    return

            if do_bind:
                self._note_tag(ace, idx, slot, uid)
                self._bind_tag(gcmd, ace, idx, slot, uid)
        finally:
            if restore:
                self._restore_spools(gcode)

    def _whose_tag(self, gcode, ace, idx, slot, reader, uid, nudge, budget):
        """Чья это метка — определяется движением, и только им.

        Мысль владельца: при заправке крутится ровно одна катушка, и по
        тому, что меняется в эфире, видно, чья метка. Здесь то же самое по
        требованию: доворачиваем свою катушку, пока метка не уйдёт из
        сектора. Не ушла в пределах бюджета — доворачиваем соседнюю: ушла,
        значит она соседская. Не ушла ни от чего — честно говорим, что не
        знаем, и не привязываем.

        Возвращает 'empty_neighbour', 'ours', 'neighbour' или 'unknown'."""
        neighbour = slot ^ 1
        if not self._slot_occupied(ace, idx, neighbour):
            # Самый частый и самый дешёвый случай: спорить не с кем.
            return 'empty_neighbour'
        for target, verdict in ((slot, 'ours'), (neighbour, 'neighbour')):
            spent = 0
            while spent < budget:
                length = min(nudge, budget - spent)
                if not self._rotate_guarded(gcode, ace, idx, target,
                                            length):
                    self._gate_lost = target
                    break
                spent += length
                if self._current_uid(ace, idx, reader) != uid:
                    return verdict
        return 'unknown'

    def _current_uid(self, ace, idx, reader):
        """Что сейчас в поле: подтверждённый UID или None."""
        uid, _distinct = self._stable_uid(ace, idx, reader)
        return uid

    def _stable_uid(self, ace, idx, reader):
        """UID, подтверждённый двумя одинаковыми чтениями подряд, и список
        всех различных меток, которые попались. UID None — не подтвердилось."""
        seen = []
        for _ in range(UID_ROUNDS):
            uid, _buf, failure = self._read_uid_once(ace, idx, reader)
            if failure is not None:
                return None, []
            if uid is not None and seen and seen[-1] == uid:
                return uid, sorted(set(u for u in seen if u))
            seen.append(uid)
        return None, sorted(set(u for u in seen if u))

    def _rotate_guarded(self, gcode, ace, idx, slot, length):
        """Довернуть катушку и проверить, что пруток остался в подаче.

        Смотать оборот и вытащить пруток из подающих шестерён — разные
        вещи. Как только гейт слота погас, крутить дальше нельзя: катушку
        придётся заправлять руками. Возвращает False, если надо
        остановиться."""
        self._rotate(gcode, slot, length)
        return self._slot_occupied(ace, idx, slot)

    def _rotate(self, gcode, slot, length):
        """Довернуть катушку подачей. Втягиванием, а не подачей вперёд:
        филамент уходит к катушке, а не к печатающей голове."""
        gcode.run_script_from_command(
            'ACE_RETRACT INDEX=%d LENGTH=%d SPEED=%d'
            % (slot, length, SEEK_SPEED))
        self._turned[slot] = self._turned.get(slot, 0) + length

    def _restore_spools(self, gcode):
        """Вернуть все провёрнутые катушки ровно туда, где они были.

        Подача вперёд ровно на смотанную длину: нетто-перемещение нулевое,
        филамент не уходит ни к голове, ни с гейта."""
        for slot, length in sorted(self._turned.items()):
            if length > 0:
                gcode.run_script_from_command(
                    'ACE_FEED INDEX=%d LENGTH=%d SPEED=%d'
                    % (slot, length, SEEK_SPEED))
        self._turned = {}

    @staticmethod
    def _slot_occupied(ace, idx, slot):
        try:
            gates = (getattr(ace, '_gate_status_per_ace', None) or {}).get(idx)
            return bool(gates and 0 <= slot < len(gates) and gates[slot] == 1)
        except Exception:
            return False

    @staticmethod
    def _note_tag(ace, idx, slot, uid):
        """Запомнить чтение метки там, где его ищет multiACE.

        Веб-часть заводит катушку в Spoolman по card_uids, опираясь на
        сохранённые чтения меток, а не на строчку в журнале. Без этой
        записи подтверждённая метка никуда не попадёт, и привязка не
        состоится, даже если UID совпадает с card_uids один в один."""
        reg = getattr(ace, '_rc_last_uid', None)
        if reg is None:
            try:
                reg = ace._rc_last_uid = {}
            except Exception:
                return
        reg[(idx, slot)] = uid
        persist = getattr(ace, '_persist_tag_reads', None)
        if persist is not None:
            try:
                persist()
            except Exception:
                pass

    def _bind_tag(self, gcmd, ace, idx, slot, uid):
        binder = getattr(ace, '_spool_bind_by_tag', None)
        if binder is None:
            gcmd.respond_info(
                '[ace_ext_raw] привязка недоступна: у multiACE нет '
                '_spool_bind_by_tag')
            return
        try:
            # unbind=False: промах по таблице не должен снимать уже
            # существующую привязку слота.
            bound = binder(idx, slot, uid, unbind=False)
        except TypeError:
            bound = binder(idx, slot, uid)
        except Exception as e:
            gcmd.respond_info('[ace_ext_raw] привязка не удалась: %s' % e)
            return
        gcmd.respond_info(
            '[ace_ext_raw] привязка: %s'
            % ('катушка %s' % bound if bound is not None
               else 'в таблице такой метки нет, катушку заведёт веб-часть'))

    def _require_fw_support(self, gcmd, ace):
        missing = [a for a in self.FW_ATTRS if not hasattr(ace, a)]
        if missing:
            raise self.printer.lookup_object('gcode').error(
                '[ace_ext_raw] эта сборка multiACE не поддерживает '
                'освобождение порта, отсутствует: %s' % ', '.join(missing))


def load_config(config):
    return AceExtRaw(config)
