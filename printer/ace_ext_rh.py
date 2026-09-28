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
# Запасной потолок температуры сушки, если у multiACE нет атрибута
# max_dryer_temperature (старая сборка) — консервативное значение по
# умолчанию в самом multiACE.
DEFAULT_MAX_DRYER_TEMP = 55
# Датчик Xiaomi LYWSD03MMC паспортно рассчитан примерно до 60 C, а сушка у
# владельца настроена на 60 C. Выше этой границы в состоянии печатается
# предупреждение: иначе температуру датчика не видит никто.
SENSOR_TEMP_WARN = 58.0

# Сколько тиков подряд повторять команду остановки, пока устройство
# отвечает «сушу». Тик идёт раз в EXT_RH_INTERVAL (60 с), то есть это
# примерно 5 минут быстрых повторов — заметно больше, чем нужно multiACE
# на переподключение USB, и несравнимо меньше 600-минутного предела
# ACE_DRY, который висит на устройстве при неудаче.
STOP_RETRY_TICKS = 5
# После исчерпания быстрых повторов модуль НЕ сдаётся (сдаться означало бы
# оставить нагреватель работать без присмотра), а переходит на редкие
# повторы: раз в столько тиков, каждый раз с записью уровня ERROR.
STOP_RETRY_SLOW_EVERY = 10
# Сколько тиков подряд наш цикл должен выглядеть «не сушит», чтобы отдать
# владение. Одного тика мало: _ace_is_drying читает кэш опроса устройства
# и отстаёт от команды запуска, так что свежий цикл выглядел бы погасшим.
IDLE_TICKS_TO_RELEASE = 3
# Сколько тиков подряд устройство должно отвечать «не сушу», чтобы считать
# остановку подтверждённой. Источник ответа — кэш статусных кадров
# (_info_per_ace): кадр без блока сушилки и первые секунды после
# переподключения читаются как «не сушу», хотя это неправда (сам multiACE
# зовёт это ловушкой, однажды уже позволившей циклу работать вечно).
# Поэтому подтверждение требует двух тиков и подключённого устройства.
STOP_CONFIRM_TICKS = 2
# Имя переменной в [save_variables], где живут незакрытые требования
# остановки. Хранится СПИСОК индексов, без текста причины: SAVE_VARIABLE
# пишет весь файл через configparser с интерполяцией, а наши причины
# содержат знак процента ('внешние 34%rH') — repr такой строки роняет
# запись файла целиком, вместе с переменными multiACE.
STOP_PENDING_VAR = 'ace_ext_rh__stop_pending'
RESTORED_WHY = 'остановка не была подтверждена до перезапуска Klipper'

# Атрибуты multiACE, без которых мы не сможем ПОГАСИТЬ наш цикл. Их
# отсутствие отключает модуль целиком. save_variable здесь потому, что
# незакрытые требования остановки обязаны переживать перезапуск.
REQUIRED_ACE_ATTRS_STOP = (
    '_auto_dry_for', '_auto_dry_stop', '_auto_dry_started',
    '_auto_dry_follow_until', '_ace_is_drying', '_is_v2', 'save_variable',
)
# Атрибуты, нужные только для ЗАПУСКА нового цикла. Их отсутствие переводит
# модуль в режим «только гашу»: новые циклы не запускаются, но ранее
# запущенные нами гасятся. Полное отключение здесь было бы опаснее, чем
# частичная работа: цикл, запущенный нами до обновления multiACE,
# восстанавливается из его хранилища, а уборка осиротевших в multiACE такой
# цикл не гасит (мастер указан и совпадает) — нагрев провисел бы остаток
# 600 минут.
REQUIRED_ACE_ATTRS_START = (
    '_auto_dry_start', '_is_actively_printing', 'auto_dry_while_printing',
    '_connected_per_ace',
)
# Атрибуты, нужные только команде НАСТРОЙКИ (ACE_EXT_RH_ENABLE). На пути
# гашения не участвуют ни одним шагом, поэтому их исчезновение деградирует
# команду, а не тик: отключать модуль целиком означало бы оставить
# восстановленный цикл догорать остаток 600 минут.
REQUIRED_ACE_ATTRS_CONFIG = ('_auto_dry_cfg', 'auto_dry_default')
REQUIRED_ACE_ATTRS = (REQUIRED_ACE_ATTRS_STOP + REQUIRED_ACE_ATTRS_START
                      + REQUIRED_ACE_ATTRS_CONFIG)


class AceExtRh:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.default_ttl = config.getint('default_ttl', DEFAULT_TTL,
                                         minval=MIN_TTL, maxval=MAX_TTL)
        self.readings = {}
        self.ace = None
        self.disabled_reason = None
        self.stop_only_reason = None
        self.no_config_reason = None
        self.timer = None
        # Незакрытые требования остановки:
        # idx -> {'why', 'ticks', 'sent', 'clear'}.
        # Живут ОТДЕЛЬНО от _auto_dry_started, потому что multiACE снимает
        # владение в _auto_dry_stop сразу и безусловно.
        self.stop_pending = {}
        # Сколько тиков подряд наш цикл выглядит «не сушит».
        self.idle_ticks = {}
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('ACE_SET_RH', self.cmd_ACE_SET_RH,
                               desc=self.cmd_ACE_SET_RH_help)
        gcode.register_command('ACE_EXT_RH_ENABLE', self.cmd_ACE_EXT_RH_ENABLE,
                               desc=self.cmd_ACE_EXT_RH_ENABLE_help)
        gcode.register_command('ACE_EXT_RH_STATUS', self.cmd_ACE_EXT_RH_STATUS,
                               desc=self.cmd_ACE_EXT_RH_STATUS_help)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)

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
        self.stop_only_reason = self._check_start_capability(ace)
        self.no_config_reason = self._check_config_capability(ace)
        self._restore_stop_pending()
        self.timer = self.reactor.register_timer(
            self._tick, self.reactor.NOW)
        if self.no_config_reason is not None:
            logging.error('[ace_ext_rh] команда ACE_EXT_RH_ENABLE недоступна: '
                          '%s. Гашение наших циклов работает как обычно.'
                          % self.no_config_reason)
        if self.stop_only_reason is not None:
            logging.error('[ace_ext_rh] готов в режиме ТОЛЬКО ГАШЕНИЕ: %s. '
                          'Новые циклы сушки не запускаются, ранее запущенные '
                          'нами — гасятся.' % self.stop_only_reason)
        else:
            logging.info('[ace_ext_rh] готов, multiACE совместим')

    def _check_compatibility(self, ace):
        if ace is None:
            return 'объект multiACE не найден'
        missing = [a for a in REQUIRED_ACE_ATTRS_STOP if not hasattr(ace, a)]
        if missing:
            return ('multiACE несовместим, отсутствует нужное для остановки '
                    'сушки: %s' % ', '.join(missing))
        return None

    def _check_start_capability(self, ace):
        missing = [a for a in REQUIRED_ACE_ATTRS_START if not hasattr(ace, a)]
        if missing:
            return ('в multiACE отсутствует нужное для запуска сушки: %s'
                    % ', '.join(missing))
        return None

    def _check_config_capability(self, ace):
        missing = [a for a in REQUIRED_ACE_ATTRS_CONFIG if not hasattr(ace, a)]
        if missing:
            return ('в multiACE отсутствует нужное для настройки автосушки: %s'
                    % ', '.join(missing))
        return None

    def _restore_stop_pending(self):
        """Поднять незакрытые требования остановки после перезапуска Klipper.

        multiACE снятие владения СОХРАНЯЕТ на диск, поэтому без этого шага
        перезапуск (а его делает и FIRMWARE_RESTART, и аварийный останов, и
        любая ошибка конфигурации) стирал бы память о требовании: владения
        нет, требования нет, устройство продолжает сушить свои 600 минут, и
        повторить остановку больше некому.
        Счётчики повторов намеренно НЕ восстанавливаются — новый экземпляр
        начинает с полной серии быстрых повторов, что безопаснее всего."""
        store = self.printer.lookup_object('save_variables', None)
        if store is None:
            return
        try:
            saved = store.allVariables.get(STOP_PENDING_VAR, None)
            if not isinstance(saved, (list, tuple, set)):
                return
            # Ключи/элементы приходят из сериализованного вида: приводим к
            # int сами и не надеемся, что тип сохранился.
            restored = sorted(set(int(i) for i in saved))
        except (AttributeError, TypeError, ValueError):
            logging.exception('[ace_ext_rh] не удалось разобрать сохранённые '
                              'требования остановки (пропущены)')
            return
        for idx in restored:
            self.stop_pending[idx] = {'why': RESTORED_WHY, 'ticks': 0,
                                      'sent': 0, 'clear': 0}
        if restored:
            logging.error('[ace_ext_rh] восстановлены НЕПОДТВЕРЖДЁННЫЕ '
                          'требования остановки для ACE %s: продолжаю '
                          'добиваться остановки нагрева.'
                          % ', '.join(str(i + 1) for i in restored))

    def _persist_stop_pending(self):
        """Сохранить состав незакрытых требований. Пишем только индексы:
        текст причины содержит знак процента, а SAVE_VARIABLE прогоняет весь
        файл переменных через configparser с интерполяцией и на таком
        значении падает, унося запись переменных multiACE вместе с нашими."""
        try:
            self.ace.save_variable(STOP_PENDING_VAR,
                                   sorted(self.stop_pending), write=True)
        except Exception as e:
            logging.warning('[ace_ext_rh] не удалось сохранить требования '
                            'остановки: %s' % e)

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
        # Уборка ВСЕГДА идёт первой, и это не вопрос вкуса. Только она
        # защищена try/except на каждое устройство и только она гасит
        # циклы, за которые больше никто не отвечает. Цикл порогов ниже
        # такой защиты не имеет: исключение из _auto_dry_start (пропавший
        # USB) обрывает его целиком. Если уборку перенести в конец, такой
        # отказ на первом устройстве оставит нагрев остальных работать до
        # следующего тика — а при устойчивом отказе и вовсе навсегда.
        self._sweep_stale(now)
        for idx in sorted(self.readings):
            rh = self.fresh_rh(idx, now)
            if rh is None:
                continue
            if idx in self.stop_pending:
                # Остановка потребована, но не подтверждена — этим занята
                # уборка, а трогать устройство вторым решением нельзя.
                continue
            cfg = ace._auto_dry_for(idx)
            if not cfg.get('enabled'):
                continue
            if self._is_ours(idx):
                if rh <= float(cfg['rh_end']):
                    self._demand_stop(idx, 'внешние %.0f%%rH' % rh)
                continue
            if self.stop_only_reason is not None:
                continue
            # Запускаем только то, что потом сможем погасить: условия здесь
            # обязаны повторять условия владения из _is_ours. Иначе на
            # настоящем ACE 2 мы запустим неостановимый цикл, а ведомого при
            # чужом мастере погасит уборка осиротевших в multiACE — и так
            # каждую минуту.
            if not self._can_start(idx):
                continue
            if ace._ace_is_drying(idx):
                continue
            if rh < float(cfg['rh_start']):
                continue
            if ace._is_actively_printing() and not ace.auto_dry_while_printing:
                continue
            ace._auto_dry_start(idx, self._start_temp(idx, cfg),
                                'внешние %.0f%%rH' % rh)

    def _start_temp(self, idx, cfg):
        """Температура запуска, ограниченная потолком устройства.
        multiACE проверяет max_dryer_temperature только в обработчике своей
        команды, но НЕ в _auto_dry_start: понизив потолок (например из-за
        риска для датчика), владелец иначе продолжал бы получать нагрев по
        старой сохранённой температуре. Потолок берём тем же способом, что и
        команда включения."""
        want = int(cfg['temp'])
        ceiling = int(getattr(self.ace, 'max_dryer_temperature',
                              DEFAULT_MAX_DRYER_TEMP))
        if want <= ceiling:
            return want
        logging.warning('[ace_ext_rh] ACE %d: сохранённая температура сушки '
                        '%d C выше потолка устройства %d C — греем на %d C'
                        % (idx + 1, want, ceiling, ceiling))
        return ceiling

    def _ownable(self, idx):
        """Общие условия владения циклом на устройстве, без подключённости:
        не ACE 2 (у него свой датчик и своя автосушка), не в окне add-time
        чужого мастера, мастер — он сам."""
        ace = self.ace
        if ace._is_v2(idx):
            return False
        if idx in ace._auto_dry_follow_until:
            # Запись здесь — настенное время multiACE (time.time()), а не
            # монотонное время реактора; сравнивать с нашим now нельзя.
            # Просроченные записи чистит сам multiACE в своём тике.
            return False
        return int(ace._auto_dry_for(idx).get('master', -1)) == idx

    def _is_ours(self, idx):
        """Наш цикл — запущенный автосушкой на не-ACE 2, который назначен
        мастером самому себе. Ручной цикл в _auto_dry_started не попадает,
        ведомый настоящего ACE 2 указывает на другой мастер.
        Подключённость здесь НЕ проверяется: гасить надо и то, что временно
        отвалилось от USB."""
        if idx not in self.ace._auto_dry_started:
            return False
        return self._ownable(idx)

    def _can_start(self, idx):
        """Можно ли запустить цикл, который мы потом сможем погасить."""
        if not self._ownable(idx):
            return False
        # Собственный тик multiACE тоже пропускает неподключённые
        # устройства: команда всё равно никуда не уйдёт.
        return bool(self.ace._connected_per_ace.get(idx, False))

    def _demand_stop(self, idx, why):
        """Потребовать остановки и ЗАПОМНИТЬ требование до подтверждения.
        Запись ставится ДО вызова: multiACE снимает владение сразу и не
        проверяет ответ устройства, а исключение из _auto_dry_stop не должно
        стереть память о том, что нагрев надо гасить."""
        self.stop_pending[idx] = {'why': why, 'ticks': 0, 'sent': 1,
                                  'clear': 0}
        self.idle_ticks.pop(idx, None)
        self._persist_stop_pending()
        self.ace._auto_dry_stop(idx, why)

    def _believed_connected(self, idx):
        """Считает ли multiACE устройство подключённым. Атрибут относится к
        группе «для запуска» и может отсутствовать: тогда проверить нечем, и
        блокировать подтверждение навсегда было бы хуже, чем поверить."""
        conn = getattr(self.ace, '_connected_per_ace', None)
        if conn is None:
            return True
        try:
            return bool(conn.get(idx, False))
        except AttributeError:
            return True

    def _follow_up_stop(self, idx):
        """Довести остановку до конца: сверить требование с фактическим
        состоянием устройства и повторить команду, пока оно отвечает «сушу».
        Повтор безопасен — drying_stop идемпотентен.

        Подтверждение принимается осторожно: только от подключённого
        устройства и только на двух тиках подряд. Ответ «не сушу» приходит
        из кэша статусных кадров и бывает ложным — кадр без блока сушилки и
        первые секунды после переподключения выглядят именно так. Ложное
        подтверждение сняло бы требование и снова открыло путь к
        работающему без присмотра нагревателю."""
        st = self.stop_pending[idx]
        drying = self.ace._ace_is_drying(idx)
        connected = self._believed_connected(idx)
        if not drying and connected:
            st['clear'] = st.get('clear', 0) + 1
            if st['clear'] < STOP_CONFIRM_TICKS:
                logging.info('[ace_ext_rh] ACE %d: похоже, не сушит (%d из %d '
                             'подтверждающих тиков)'
                             % (idx + 1, st['clear'], STOP_CONFIRM_TICKS))
                return
            del self.stop_pending[idx]
            self.idle_ticks.pop(idx, None)
            self._persist_stop_pending()
            logging.info('[ace_ext_rh] ACE %d: остановка подтверждена '
                         '(%s, команд: %d)' % (idx + 1, st['why'], st['sent']))
            return
        st['clear'] = 0
        st['ticks'] += 1
        ticks = st['ticks']
        if ticks <= STOP_RETRY_TICKS:
            st['sent'] += 1
            logging.warning('[ace_ext_rh] ACE %d через %d тик(ов) после '
                            'остановки не подтвердил её (сушит=%s, '
                            'подключено=%s) — повторяю команду (%d из %d)'
                            % (idx + 1, ticks, drying, connected,
                               ticks, STOP_RETRY_TICKS))
            self.ace._auto_dry_stop(idx, '%s (повтор %d)' % (st['why'], ticks))
            return
        # Быстрые повторы исчерпаны. Сдаться нельзя: на устройстве висит
        # duration=600 минут, и погасить его больше некому. Поэтому громкая
        # запись и редкие повторы до подтверждения.
        overdue = ticks - STOP_RETRY_TICKS
        if overdue != 1 and overdue % STOP_RETRY_SLOW_EVERY != 0:
            return
        st['sent'] += 1
        logging.error(
            '[ace_ext_rh] ACE %d НЕ ПОДТВЕРДИЛ ОСТАНОВКУ: %d команд(ы) за '
            '%d мин (сушит=%s, подключено=%s). Нагреватель может работать '
            'без присмотра (предел ACE_DRY — 600 минут) — проверьте сушилку '
            'и связь с ней. Повторяю остановку раз в %d мин, пока устройство '
            'не подтвердит.'
            % (idx + 1, st['sent'], int(ticks * EXT_RH_INTERVAL // 60),
               drying, connected,
               int(STOP_RETRY_SLOW_EVERY * EXT_RH_INTERVAL // 60)))
        self.ace._auto_dry_stop(idx, '%s (повтор %d)' % (st['why'], ticks))

    def _watch_for_self_stop(self, idx):
        """Наш цикл, который устройство больше не выполняет. Так выглядит
        исчерпание 600-минутного предела ACE_DRY: устройство остановилось
        само, а multiACE по-прежнему считает цикл нашим. Владение надо
        отдать, иначе автоматика залипнет — ветка остановки только гасит, а
        ветка запуска требует «не наш»."""
        if self.ace._ace_is_drying(idx):
            self.idle_ticks.pop(idx, None)
            return
        seen = self.idle_ticks.get(idx, 0) + 1
        self.idle_ticks[idx] = seen
        if seen < IDLE_TICKS_TO_RELEASE:
            return
        self._demand_stop(idx, 'устройство не сушит %d тика подряд '
                               '(предел ACE_DRY исчерпан?) — отдаём владение'
                               % seen)

    def _sweep_stale(self, now):
        """Гасит всё, за что отвечаем мы, и доводит остановку до конца.

        Три обязанности:
        1. Незакрытые требования остановки. multiACE в _auto_dry_stop
           снимает владение СРАЗУ и не проверяет, послушалось ли
           устройство: ненулевой ответ попадает только в журнал. Потерянный
           на USB пакет оставил бы нагреватель на все 600 минут, причём
           повторить остановку было бы уже некому — _is_ours навсегда
           False. Поэтому требования живут в self.stop_pending и обходятся
           отдельно от _auto_dry_started.
        2. Гашение наших циклов, когда автосушку выключили в настройках
           multiACE или показание протухло/отсутствует (в том числе сразу
           после рестарта Klipper, пока показаний ещё не было ни одного).
           _auto_dry_start командует устройству duration=600 минут: само
           оно не остановится.
        3. Отдача владения, когда устройство перестало сушить само.

        Ошибка на одном устройстве (пропавший USB и т.п.) не должна мешать
        погасить остальные — поэтому каждая итерация в своём try/except,
        включая вызовы _is_ours и _ace_is_drying: они тоже обращаются к
        чужому объекту и тоже могут бросить именно в этом сценарии."""
        ace = self.ace
        # Счётчик простоя имеет смысл только пока цикл наш, а владение можно
        # потерять и вне наших путей — например ручной остановкой сушилки в
        # multiACE. Поэтому чистим по ФАКТУ отсутствия владения, а не на
        # известных путях: подвисший счётчик обваливает трёхтиковый запас и
        # гасит следующий, только что запущенный цикл через один тик
        # задержки кэша, да ещё и лжёт в журнале про «не сушит 3 тика».
        for idx in [i for i in self.idle_ticks
                    if i not in ace._auto_dry_started]:
            del self.idle_ticks[idx]
        for idx in sorted(self.stop_pending):
            try:
                self._follow_up_stop(idx)
            except Exception:
                logging.exception('[ace_ext_rh] ошибка подтверждения '
                                  'остановки ACE %d (пропущена)' % (idx + 1))
        for idx in sorted(ace._auto_dry_started):
            try:
                if not self._is_ours(idx):
                    continue
                if idx in self.stop_pending:
                    continue
                if not ace._auto_dry_for(idx).get('enabled'):
                    self._demand_stop(idx, 'автосушка выключена')
                    continue
                if self.fresh_rh(idx, now) is None:
                    self._demand_stop(idx, 'показание влажности протухло')
                    continue
                self._watch_for_self_stop(idx)
            except Exception:
                logging.exception(
                    '[ace_ext_rh] ошибка остановки ACE %d (пропущена)'
                    % (idx + 1))

    cmd_ACE_EXT_RH_ENABLE_help = (
        '[ace_ext_rh] Включить автосушку по внешней влажности для ACE Pro: '
        'ACE_EXT_RH_ENABLE ACE=0 ENABLE=1 [RH_START=45] [RH_END=35] [TEMP=50]')

    def cmd_ACE_EXT_RH_ENABLE(self, gcmd):
        ace = self._require_ace(gcmd)
        if self.no_config_reason is not None:
            raise self.printer.lookup_object('gcode').error(
                '[ace_ext_rh] настройка автосушки недоступна: %s. Тик модуля '
                'при этом работает и наши циклы гасит.'
                % self.no_config_reason)
        idx = gcmd.get_int('ACE', 0, minval=0, maxval=3)
        enable = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        key = str(idx)
        cur = dict(ace._auto_dry_cfg.get(key, {}))
        cur['enabled'] = enable
        cur['master'] = idx
        # Потолок TEMP берём из настройки самого принтера: у multiACE по
        # умолчанию 55, но на конкретной машине может быть выше (или ниже).
        # getattr с запасным значением — на случай сборки multiACE, где
        # этого атрибута вовсе нет; max_dryer_temperature намеренно не
        # входит в REQUIRED_ACE_ATTRS, так как необязателен.
        max_temp = getattr(ace, 'max_dryer_temperature', DEFAULT_MAX_DRYER_TEMP)
        for param, name, cast, lo, hi in (
                ('RH_START', 'rh_start', float, 5., 95.),
                ('RH_END', 'rh_end', float, 1., 94.),
                ('TEMP', 'temp', int, 35., max_temp)):
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
            logging.warning('[ace_ext_rh] не удалось сохранить настройки: %s' % e)
        if not enable and self._is_ours(idx):
            self._demand_stop(idx, 'автосушка выключена')
        gcmd.respond_info(
            '[ace_ext_rh] ACE %d: автосушка %s, старт >= %.0f%%, стоп <= '
            '%.0f%%, %d C' % (idx + 1, 'ВКЛ' if enable else 'ВЫКЛ',
                              float(merged['rh_start']),
                              float(merged['rh_end']), int(merged['temp'])))
        if enable and self.stop_only_reason is not None:
            gcmd.respond_info(
                '[ace_ext_rh] ВНИМАНИЕ: режим ТОЛЬКО ГАШЕНИЕ (%s) — новые '
                'циклы сушки запускаться НЕ будут' % self.stop_only_reason)

    cmd_ACE_EXT_RH_STATUS_help = (
        '[ace_ext_rh] Показать внешнюю влажность, её возраст и состояние')

    def cmd_ACE_EXT_RH_STATUS(self, gcmd):
        if self.disabled_reason is not None:
            gcmd.respond_info('[ace_ext_rh] ОТКЛЮЧЁН: %s'
                              % self.disabled_reason)
            return
        if self.stop_only_reason is not None:
            gcmd.respond_info(
                '[ace_ext_rh] режим ТОЛЬКО ГАШЕНИЕ: %s. Новые циклы не '
                'запускаются, ранее запущенные нами — гасятся.'
                % self.stop_only_reason)
        if self.no_config_reason is not None:
            gcmd.respond_info(
                '[ace_ext_rh] ВНИМАНИЕ: настройка автосушки недоступна: %s'
                % self.no_config_reason)
        # Незакрытые требования остановки печатаются ДО показаний и вне их
        # обхода: самый частый путь их появления — протухание сразу после
        # перезапуска Klipper, когда показаний ещё не было ни одного, и
        # ранний выход «показаний ещё не поступало» их бы скрыл — ровно
        # тогда, когда они важнее всего.
        for idx in sorted(self.stop_pending):
            st = self.stop_pending[idx]
            gcmd.respond_info(
                '[ace_ext_rh] ВНИМАНИЕ: ACE %d не подтвердил остановку '
                '(%s): команд %d, тиков ожидания %d, подтверждающих тиков '
                '%d из %d'
                % (idx + 1, st['why'], st['sent'], st['ticks'],
                   st.get('clear', 0), STOP_CONFIRM_TICKS))
        now = self.reactor.monotonic()
        if not self.readings:
            gcmd.respond_info('[ace_ext_rh] показаний ещё не поступало')
            return
        for idx in sorted(self.readings):
            r = self.readings[idx]
            cfg = self.ace._auto_dry_for(idx)
            age = int(now - r['at'])
            gcmd.respond_info(
                '[ace_ext_rh] ACE %d: %.1f%%rH, возраст %d с, %s | '
                'температура датчика %s | автосушка %s, старт >= %.0f%%, '
                'стоп <= %.0f%%, %d C | цикл наш: %s'
                % (idx + 1, r['rh'], age,
                   'годно' if now < r['expires'] else 'ПРОТУХЛО',
                   ('%.1f C (возраст %d с)' % (r['temp'], age))
                   if r['temp'] is not None else 'не передана',
                   'ВКЛ' if cfg.get('enabled') else 'ВЫКЛ',
                   float(cfg['rh_start']), float(cfg['rh_end']),
                   int(cfg['temp']), 'да' if self._is_ours(idx) else 'нет'))
            if r['temp'] is not None and r['temp'] > SENSOR_TEMP_WARN:
                gcmd.respond_info(
                    '[ace_ext_rh] ВНИМАНИЕ: ACE %d, температура датчика '
                    '%.1f C выше %.0f C. Датчик LYWSD03MMC паспортно '
                    'рассчитан примерно до 60 C — проверьте место крепления '
                    'и температуру сушки.'
                    % (idx + 1, r['temp'], SENSOR_TEMP_WARN))

    def _require_ace(self, gcmd):
        if self.ace is None:
            raise self.printer.lookup_object('gcode').error(
                '[ace_ext_rh] отключён: %s' % self.disabled_reason)
        return self.ace


def load_config(config):
    return AceExtRh(config)
