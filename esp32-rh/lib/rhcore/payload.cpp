#include "payload.h"

namespace rhcore {

bool parse_lywsd(const uint8_t* data, size_t len, Reading& out) {
    if (data == nullptr || len < 5) {
        return false;
    }
    const uint8_t rh = data[2];
    if (rh > 100) {
        return false;
    }
    // Явная сборка из байтов вместо memcpy/reinterpret_cast: не зависит от
    // порядка байтов платформы, а датчик всегда отдаёт little-endian.
    const uint16_t raw_temp = static_cast<uint16_t>(data[0]) |
                              (static_cast<uint16_t>(data[1]) << 8);
    out.temp_centi_c = static_cast<int16_t>(raw_temp);
    out.rh_percent = rh;
    out.battery_mv = static_cast<uint16_t>(data[3]) |
                     (static_cast<uint16_t>(data[4]) << 8);
    return true;
}

}  // namespace rhcore
