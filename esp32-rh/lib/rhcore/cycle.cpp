#include "cycle.h"

namespace rhcore {

Cycle::Cycle(const CycleConfig& cfg, uint32_t now_ms)
    : cfg_(cfg),
      last_start_ms_(now_ms),
      // Отсчёт молчания идёт с момента включения: плата, которая ни разу
      // ничего не доставила, тоже обязана перезагрузиться.
      last_delivered_ms_(now_ms),
      started_(false),
      failures_(0) {}

bool Cycle::due(uint32_t now_ms) const {
    if (!started_) {
        return true;
    }
    return static_cast<uint32_t>(now_ms - last_start_ms_) >= cfg_.interval_ms;
}

void Cycle::mark_started(uint32_t now_ms) {
    last_start_ms_ = now_ms;
    started_ = true;
}

uint32_t Cycle::silence_ms(uint32_t now_ms) const {
    return static_cast<uint32_t>(now_ms - last_delivered_ms_);
}

Action Cycle::check_silence(uint32_t now_ms) const {
    if (silence_ms(now_ms) >= cfg_.reboot_after_silence_ms) {
        return Action::Reboot;
    }
    return Action::None;
}

Action Cycle::on_read_failed(uint32_t now_ms) {
    if (failures_ < UINT8_MAX) {
        ++failures_;
    }
    // Перезагрузка важнее переинициализации BLE: если молчание затянулось,
    // стек BLE уже пробовали поднимать и это не помогло.
    if (check_silence(now_ms) == Action::Reboot) {
        return Action::Reboot;
    }
    if (failures_ >= cfg_.failures_before_ble_reset) {
        failures_ = 0;
        return Action::ResetBle;
    }
    return Action::None;
}

Action Cycle::on_delivered(uint32_t now_ms) {
    failures_ = 0;
    last_delivered_ms_ = now_ms;
    return Action::None;
}

Action Cycle::on_not_delivered(uint32_t now_ms) {
    failures_ = 0;
    return check_silence(now_ms);
}

}  // namespace rhcore
