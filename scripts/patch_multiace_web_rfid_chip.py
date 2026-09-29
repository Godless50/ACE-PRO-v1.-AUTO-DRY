#!/usr/bin/env python3
"""Чип «RFID» на плитке слота в панели multiACE.

Зачем: у gen1 ACE Pro метка читается (в статусе слота rfid=2 + rfid_data с
кодом метки), но плитка показывает только бейдж связанного спула Spoolman
(«SM»), который по коду ЗАМЕНЯЕТ бейдж происхождения. В итоге не видно, что
пластик распознан по метке.

Здесь бейджи плитки собираются в одну строку: чип «RFID» (штатный класс
.src-rfid, подсказка — формат метки и её код) плюс, как и раньше, бейдж спула
или бейдж происхождения (Set/Job/UID). Вид пустых и UID-only слотов не
меняется.

Правятся ФРОНТЕНД-файлы на принтере (статика; klippy перезапускать не нужно,
достаточно обновить страницу):
    frontend/index.html  — блок бейджей плитки
    frontend/app.js      — функция rfidChipTitle() и её экспорт в шаблон
    frontend/style.css   — строка бейджей (.slot-badges)

Каждая правка сверяется с точным исходным текстом; при несовпадении скрипт
останавливается, ничего не записав. Резервные копии: *.orig_before_rfid_chip

Запуск на принтере:
    python3 patch_multiace_web_rfid_chip.py [/home/lava/multiace_web/frontend]
"""
import io
import os
import shutil
import sys

HTML_OLD = """                  <!-- A bound spool REPLACES the rfid/set badge: it is the
                       more specific statement (this exact spool, with its
                       weight), and the tag/override is what got it bound. -->
                  <em v-if="spoolForSlot(ace.idx, slot.idx)"
                      class="src-badge"
                      :class="spoolBadgeCls(spoolForSlot(ace.idx, slot.idx))"
                      :title="spoolTitle(spoolForSlot(ace.idx, slot.idx))">{{
                      spoolBadgeLabel(spoolForSlot(ace.idx, slot.idx)) }}</em>
                  <em v-else-if="slot.material && sourceLabel(slot.source)"
                      class="src-badge" :class="'src-' + slot.source">{{ sourceLabel(slot.source) }}</em>"""

HTML_NEW = """                  <!-- Badge row at the tile bottom: a live tag read is
                       stated first (RFID), then the bound spool / override -
                       the tag no longer disappears behind the Spoolman SM. -->
                  <span v-if="slot.rfid === 2
                                || spoolForSlot(ace.idx, slot.idx)"
                        class="slot-badges">
                    <!-- Метка прочитана устройством: плитка говорит об этом
                         прямо, независимо от того, привязан ли спул. -->
                    <em v-if="slot.rfid === 2" class="src-badge src-rfid"
                        :title="rfidChipTitle(slot)">{{ sourceLabel('rfid') }}</em>
                    <em v-if="spoolForSlot(ace.idx, slot.idx)"
                        class="src-badge"
                        :class="spoolBadgeCls(spoolForSlot(ace.idx, slot.idx))"
                        :title="spoolTitle(spoolForSlot(ace.idx, slot.idx))">{{
                        spoolBadgeLabel(spoolForSlot(ace.idx, slot.idx)) }}</em>
                    <em v-else-if="slot.material && sourceLabel(slot.source)"
                        class="src-badge" :class="'src-' + slot.source">{{ sourceLabel(slot.source) }}</em>
                  </span>
                  <em v-else-if="slot.material && sourceLabel(slot.source)"
                      class="src-badge" :class="'src-' + slot.source">{{ sourceLabel(slot.source) }}</em>"""

JS_ANCHOR = """    function sourceLabel(src) {
      if (src === "rfid") return t("ui.common.source_rfid");
      if (src === "override") return t("ui.common.source_override");
      if (src === "derived") return t("ui.common.source_derived");
      return "";
    }"""

JS_NEW = JS_ANCHOR + """
    // Подсказка чипа «RFID» на плитке: что именно прочитано с метки -
    // формат и код (SKU), для метки без личности - её UID.
    function rfidChipTitle(slot) {
      const r = (slot && slot.rfid_data) || {};
      const code = String(r.sku || (slot && slot.sku)
                          || (slot && slot.uid) || "").trim();
      const fmt = tagFormatLabel(slot && slot.tag_format);
      return [t("ui.common.source_rfid"), fmt, code]
        .filter(Boolean).join(" · ");
    }"""

JS_EXPORT_OLD = "      spoolBadgeCls, spoolBadgeLabel, "
JS_EXPORT_NEW = "      spoolBadgeCls, spoolBadgeLabel, rfidChipTitle, "

CSS_ANCHOR = "button.slot em.src-badge { margin-top: auto; margin-left: 0; }"
CSS_NEW = CSS_ANCHOR + """
/* Строка бейджей плитки: чип «RFID» и бейдж спула стоят рядом, а не друг
   под другом. Отступ снизу держит строка, поэтому у самих бейджей его нет. */
button.slot .slot-badges { display: flex; align-items: center;
  justify-content: center; gap: .15rem; margin-top: auto; }
button.slot .slot-badges em.src-badge { margin-top: 0; margin-left: 0; }"""


def _patch(path, edits, marker):
    s = io.open(path, encoding='utf-8').read()
    if marker in s:
        return 'уже пропатчен'
    for old, new in edits:
        if old not in s:
            raise SystemExit('ОСТАНОВЛЕНО: не найден фрагмент в %s:\n%s'
                             % (path, old.splitlines()[0]))
    shutil.copy(path, path + '.orig_before_rfid_chip')
    for old, new in edits:
        s = s.replace(old, new, 1)
    io.open(path, 'w', encoding='utf-8').write(s)
    return 'правок: %d' % len(edits)


def main(root):
    print('index.html: %s' % _patch(os.path.join(root, 'index.html'),
                                    [(HTML_OLD, HTML_NEW)], 'slot-badges'))
    print('app.js:     %s' % _patch(os.path.join(root, 'app.js'),
                                    [(JS_ANCHOR, JS_NEW),
                                     (JS_EXPORT_OLD, JS_EXPORT_NEW)],
                                    'function rfidChipTitle'))
    print('style.css:  %s' % _patch(os.path.join(root, 'style.css'),
                                    [(CSS_ANCHOR, CSS_NEW)],
                                    '.slot-badges { display: flex;'))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1
                  else '/home/lava/multiace_web/frontend'))
