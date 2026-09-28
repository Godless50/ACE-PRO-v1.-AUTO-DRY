#!/usr/bin/env python3
"""Снять с multiACE ограничение «автосушка ACE 1 только под управлением ACE 2».

multiACE считает, что влажность умеет мерить только ACE 2, поэтому на
одиночном ACE 1 автосушку нельзя ни включить, ни выключить из панели:
проверка мастера отказывает с «ACE 1 is not an ACE 2». У нас влажность
даёт внешний датчик (ace_ext_rh), и ACE 1 может быть мастером самому себе.

Правится файл на принтере; резервная копия кладётся рядом с суффиксом
.orig_before_ace1_patch. Обновление multiACE перезапишет ace.py -- тогда
запустить скрипт снова. Каждая правка проверяется на точное совпадение
исходного текста: если multiACE изменилась, скрипт остановится, а не
испортит файл.

Запуск на принтере:  python3 patch_multiace_ace1.py /home/lava/klipper/klippy/extras/ace.py
"""
import io
import shutil
import sys

EDITS = [
    # 1. Мастером может быть любое устройство, не только ACE 2.
    ("""            if v is not None:
                if v >= 0 and not self._is_v2(v):
                    raise self._ace_error(
                        gcmd, 'MASTER: ACE %d is not an ACE 2 - only an ACE 2 '
                              'reports humidity and can drive a follower'
                              % self._disp(v), code=200)
                cur['master'] = v""",
     """            if v is not None:
                # ace_ext_rh: ACE 1 даёт влажность с внешнего датчика и
                # может быть мастером самому себе. Ограничение «только ACE 2»
                # снято по решению владельца.
                cur['master'] = v"""),
    # 2. Без мастера ACE 1 становится мастером сам себе, а не выключается.
    ("""        if (not is_v2 and eff.get('enabled')
                and int(eff.get('master', -1)) < 0):
            cur = dict(self._auto_dry_cfg.get(key, {}))
            cur['enabled'] = False
            self._auto_dry_cfg[key] = cur
            raise self._ace_error(
                gcmd, 'ACE %d has no master - pick the ACE 2 that drives it '
                      'before switching auto-dry on (MASTER=<ace>)'
                      % self._disp(idx), code=200)
""",
     """        if (not is_v2 and eff.get('enabled')
                and int(eff.get('master', -1)) < 0):
            # ace_ext_rh: без мастера ACE 1 становится мастером сам себе —
            # влажность приходит с внешнего датчика.
            cur = dict(self._auto_dry_cfg.get(key, {}))
            cur['master'] = idx
            self._auto_dry_cfg[key] = cur
"""),
    # 3. Пороги RH_START/RH_END разрешены и для ACE 1.
    ("""        _wrong = ([p for p in ('MASTER', 'ADD_TIME') if gcmd.get(p, None) is not None]
                  if is_v2 else
                  [p for p in ('RH_START', 'RH_END') if gcmd.get(p, None) is not None])""",
     """        _wrong = ([p for p in ('MASTER', 'ADD_TIME') if gcmd.get(p, None) is not None]
                  if is_v2 else [])  # ace_ext_rh: ACE 1 сам задаёт пороги"""),
    # 4. Панель предлагает мастером и ACE 1.
    ("""        auto_dry_masters = [i for i in range(len(self._ace_devices))
                            if self._connected_per_ace.get(i, False)
                            and self._is_v2(i)]""",
     """        auto_dry_masters = [i for i in range(len(self._ace_devices))
                            if self._connected_per_ace.get(i, False)]"""),
]


def main(path):
    s = io.open(path, encoding='utf-8').read()
    if 'ace_ext_rh: ACE 1' in s:
        print('патч уже стоит')
        return 0
    for old, new in EDITS:
        if old not in s:
            print('ОСТАНОВЛЕНО: исходный фрагмент не найден, multiACE изменилась:')
            print(old.splitlines()[0])
            return 1
    shutil.copy(path, path + '.orig_before_ace1_patch')
    for old, new in EDITS:
        s = s.replace(old, new, 1)
    io.open(path, 'w', encoding='utf-8').write(s)
    print('правок применено: %d; резервная копия рядом' % len(EDITS))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                  '/home/lava/klipper/klippy/extras/ace.py'))
