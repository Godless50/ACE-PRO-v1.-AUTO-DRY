#include "gcode.h"

#include <stdio.h>
#include <stdlib.h>

namespace rhcore {

int16_t round_centi_to_deci(int16_t centi) {
    // Округление «половина от нуля» на целых: 235 -> 24, -235 -> -24.
    // Делать это через double нельзя, см. пояснение в reading.h.
    const int32_t v = centi;
    const int32_t shifted = v >= 0 ? v + 5 : v - 5;
    return static_cast<int16_t>(shifted / 10);
}

size_t build_set_rh(const Reading& r, int ace_index, int ttl_seconds,
                    char* buf, size_t buf_size) {
    if (buf == nullptr || buf_size == 0) {
        return 0;
    }
    if (ace_index < 0 || ace_index > 3) {
        return 0;
    }
    // Те же границы, что проверяет модуль на принтере (ace_ext_rh.py):
    // показание вне диапазона он отвергнет, поэтому не отправляем его
    // вовсе, чтобы не получить ошибку в журнале принтера.
    if (r.rh_percent > 100) {
        return 0;
    }
    if (ttl_seconds < 60 || ttl_seconds > 7200) {
        return 0;
    }

    const int16_t deci = round_centi_to_deci(r.temp_centi_c);
    const int whole = deci / 10;
    const int frac = abs(deci % 10);
    // Знак минус надо печатать отдельно: при -0.4 целая часть равна нулю и
    // сама знака не несёт.
    const char* sign = (deci < 0 && whole == 0) ? "-" : "";

    const int written = snprintf(
        buf, buf_size,
        "ACE_SET_RH ACE=%d RH=%u.0 TTL=%d TEMP=%s%d.%d",
        ace_index, static_cast<unsigned>(r.rh_percent), ttl_seconds,
        sign, whole, frac);
    if (written < 0 || static_cast<size_t>(written) >= buf_size) {
        return 0;
    }
    return static_cast<size_t>(written);
}

}  // namespace rhcore
