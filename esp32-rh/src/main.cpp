// ESP32 внутри корпуса ACE Pro: читает Xiaomi LYWSD03MMC по BLE и передаёт
// влажность в Moonraker командой ACE_SET_RH.
//
// ГЛАВНОЕ ПРАВИЛО БЕЗОПАСНОСТИ. При неудачном чтении датчика не
// отправляется НИЧЕГО. Старое значение продлило бы срок годности и
// оставило нагреватель включённым вслепую: команда запуска сушки уходит в
// ACE с длительностью 600 минут, само устройство не остановится. Молчание
// же через RH_TTL_SECONDS заставит модуль ace_ext_rh.py на принтере погасить
// нагрев. Севшая батарейка датчика, зависший Wi-Fi или сгоревшая плата
// приводят к остановке сушки, а не к бесконечному нагреву.
//
// Решения о нагреве принимает штатный механизм автосушки multiACE. Эта
// прошивка — курьер без собственной логики, как и серверный контейнер,
// который она заменяет.
#include <Arduino.h>
#include <esp_task_wdt.h>

#include "ble_sensor.h"
#include "config.h"
#include "cycle.h"
#include "gcode.h"
#include "net.h"
#include "payload.h"

namespace {

// Сторожевой таймер с запасом: одно чтение датчика в худшем случае занимает
// около 20 секунд, плюс запрос к Moonraker до 15 секунд.
constexpr uint32_t kWatchdogSeconds = 90;
constexpr uint32_t kSensorTimeoutMs = 20000;
constexpr uint32_t kWifiWaitMs = 30000;

BleSensor g_sensor;
Net g_net;

rhcore::CycleConfig make_cycle_config() {
    rhcore::CycleConfig cfg;
    cfg.interval_ms = static_cast<uint32_t>(INTERVAL_SECONDS) * 1000u;
    // Перезагрузка раньше, чем истечёт срок годности показания: после
    // перезапуска должно остаться время доставить свежее значение до того,
    // как принтер погасит нагрев по протуханию.
    const uint32_t ttl_ms = static_cast<uint32_t>(RH_TTL_SECONDS) * 1000u;
    cfg.reboot_after_silence_ms = ttl_ms - ttl_ms / 3;
    return cfg;
}

// Определяется в setup(), когда известно время старта.
rhcore::Cycle* g_cycle = nullptr;

void apply(rhcore::Action action) {
    switch (action) {
        case rhcore::Action::None:
            break;
        case rhcore::Action::ResetBle:
            g_net.publish_event("ble_reset", "неудачные чтения подряд");
            Serial.println("[ace-rh] переинициализация стека BLE");
            g_sensor.reset_stack();
            break;
        case rhcore::Action::Reboot:
            g_net.publish_event("reboot", "слишком долго нет доставленных показаний");
            Serial.println("[ace-rh] перезагрузка: показания не доставляются");
            delay(500);  // дать MQTT и журналу уйти в сеть
            ESP.restart();
            break;
    }
}

void run_cycle(uint32_t now) {
    g_cycle->mark_started(now);

    rhcore::Reading reading{};
    if (!g_sensor.read(reading, kSensorTimeoutMs)) {
        const uint32_t t = millis();
        const rhcore::Action action = g_cycle->on_read_failed(t);
        g_net.set_status(nullptr, g_cycle->silence_ms(t),
                         g_cycle->consecutive_failures());
        g_net.publish_event("read_failed", "датчик не отвечает");
        Serial.printf("[ace-rh] датчик не прочитан, неудач подряд: %u\n",
                      g_cycle->consecutive_failures());
        apply(action);
        return;
    }

    char script[rhcore::kGcodeBufSize];
    const size_t n = rhcore::build_set_rh(reading, ACE_INDEX, RH_TTL_SECONDS,
                                          script, sizeof(script));
    // n == 0 означает негодные данные или тесный буфер. Отправлять нечего:
    // лучше молчание, которое погасит нагрев, чем сомнительная команда.
    const bool delivered = (n > 0) && g_net.send_gcode(script);

    const uint32_t t = millis();
    const rhcore::Action action = delivered ? g_cycle->on_delivered(t)
                                           : g_cycle->on_not_delivered(t);
    g_net.set_status(delivered ? script : nullptr, g_cycle->silence_ms(t),
                     g_cycle->consecutive_failures());
    g_net.publish_reading(reading, delivered);
    Serial.printf("[ace-rh] %u%%rH, %d.%02d C, батарея %u мВ -> принтер: %s\n",
                  static_cast<unsigned>(reading.rh_percent),
                  reading.temp_centi_c / 100, abs(reading.temp_centi_c % 100),
                  static_cast<unsigned>(reading.battery_mv),
                  delivered ? "принято" : "НЕ ДОСТАВЛЕНО");
    apply(action);
}

}  // namespace

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println();
    Serial.println("[ace-rh] запуск");

    esp_task_wdt_init(kWatchdogSeconds, true);
    esp_task_wdt_add(nullptr);

    g_net.begin();
    // Ждём сеть, но не бесконечно: без Wi-Fi прошивка всё равно должна
    // работать и копить молчание, чтобы в итоге перезагрузиться.
    const uint32_t deadline = millis() + kWifiWaitMs;
    while (!g_net.wifi_ready() && static_cast<int32_t>(deadline - millis()) > 0) {
        g_net.loop();
        esp_task_wdt_reset();
        delay(200);
    }
    Serial.printf("[ace-rh] Wi-Fi: %s\n",
                  g_net.wifi_ready() ? "подключён" : "НЕТ СЕТИ");

    g_sensor.begin();

    static rhcore::Cycle cycle(make_cycle_config(), millis());
    g_cycle = &cycle;

    g_net.publish_event("boot", "прошивка запущена");
}

void loop() {
    esp_task_wdt_reset();
    g_net.loop();

    const uint32_t now = millis();
    if (g_cycle->due(now)) {
        run_cycle(now);
    }
    delay(50);
}
