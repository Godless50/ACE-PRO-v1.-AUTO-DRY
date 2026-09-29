#!/usr/bin/env python3
"""Подписи в панели автосушки multiACE: мастер — «ACEpro_v.1».

Зачем: Gen-1 ACE Pro умеет быть мастером самому себе (влажность даёт
внешний датчик, модуль ace_ext_rh), но в списке мастеров он выводился
безымянным «ACE 1» — как слот, а не как устройство. Здесь список
мастеров получает собственную подпись.

Правится ФРОНТЕНД на принтере (статика, klippy перезапускать не нужно —
достаточно обновить страницу в браузере):
    /home/lava/multiace_web/frontend/index.html   — шаблон выпадающего списка
    /home/lava/multiace_web/frontend/app.js       — функция autoDryMasterLabel()

Каждая правка сверяется с точным исходным текстом: если версия multiACE-web
изменилась, скрипт останавливается, ничего не записав. Резервные копии —
рядом с файлами, суффикс .orig_before_gen1_label.

Запуск на принтере:
    python3 patch_multiace_web_gen1.py [/home/lava/multiace_web/frontend]
"""
import io
import os
import shutil
import sys

OPTION_OLD = """                      <option v-for="m in autoDryMasters()" :key="m" :value="m">
                        ACE {{ m + 1 }}</option>"""

OPTION_NEW = """                      <option v-for="m in autoDryMasters()" :key="m" :value="m">
                        {{ autoDryMasterLabel(m) }}</option>"""

HELPER_ANCHOR = "    const autoDryMasters = () => state.auto_dry_masters || [];"

HELPER_NEW = HELPER_ANCHOR + """
    // Подпись устройства в списке мастеров. Gen-1 ACE Pro (protocol v1)
    // сушит себя сам по внешнему датчику (ace_ext_rh), поэтому называется
    // по делу: ACEpro_v.1. ACE 2 остаётся «ACE N».
    function autoDryMasterLabel(idx) {
      const a = (state.aces || []).find(x => Number(x.idx) === Number(idx));
      if (a && a.protocol !== "v2") return "ACEpro_v.1";
      return "ACE " + (Number(idx) + 1);
    }"""

EXPORT_OLD = "autoDryEnable, autoDrySetMaster, autoDryMasters, spoolmanUrl,"
EXPORT_NEW = ("autoDryEnable, autoDrySetMaster, autoDryMasters, "
              "autoDryMasterLabel, spoolmanUrl,")


def _patch(path, edits):
    s = io.open(path, encoding='utf-8').read()
    if 'autoDryMasterLabel' in s and path.endswith('index.html'):
        return 'уже пропатчен'
    if 'function autoDryMasterLabel' in s and path.endswith('app.js'):
        return 'уже пропатчен'
    for old, new in edits:
        if old not in s:
            raise SystemExit('ОСТАНОВЛЕНО: не найден фрагмент в %s:\n%s'
                             % (path, old.splitlines()[0]))
    shutil.copy(path, path + '.orig_before_gen1_label')
    for old, new in edits:
        s = s.replace(old, new, 1)
    io.open(path, 'w', encoding='utf-8').write(s)
    return 'правок: %d' % len(edits)


def main(root):
    html = os.path.join(root, 'index.html')
    js = os.path.join(root, 'app.js')
    print('index.html: %s' % _patch(html, [(OPTION_OLD, OPTION_NEW)]))
    print('app.js:     %s' % _patch(js, [(HELPER_ANCHOR, HELPER_NEW),
                                         (EXPORT_OLD, EXPORT_NEW)]))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1
                  else '/home/lava/multiace_web/frontend'))
