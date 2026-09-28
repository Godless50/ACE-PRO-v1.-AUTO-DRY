#include "ble_sensor.h"

#include <Arduino.h>
#include <NimBLEDevice.h>

#include "config.h"

#include "payload.h"

namespace {

// Служба и характеристика уведомлений LYWSD03MMC.
const NimBLEUUID kService("ebe0ccb0-7a0a-4b0c-8a1a-6ff2997da3a6");
const NimBLEUUID kCharacteristic("ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6");

// Приёмник уведомления. Статический, потому что обратный вызов NimBLE не
// принимает пользовательский указатель в этой версии библиотеки.
volatile bool g_have_reading = false;
rhcore::Reading g_reading{};

void on_notify(NimBLERemoteCharacteristic*, uint8_t* data, size_t length,
               bool) {
    if (g_have_reading) {
        return;  // первое годное показание уже взяли
    }
    rhcore::Reading parsed{};
    if (rhcore::parse_lywsd(data, length, parsed)) {
        g_reading = parsed;
        g_have_reading = true;
    }
}

// Клиент создаётся один раз и переиспользуется. Создавать и удалять клиента
// на каждом опросе нельзя: куча фрагментируется и через несколько суток
// подключения начинают падать.
NimBLEClient* client() {
    static NimBLEClient* c = nullptr;
    if (c == nullptr) {
        c = NimBLEDevice::createClient();
        c->setConnectTimeout(10);
    }
    return c;
}

}  // namespace

void BleSensor::begin() {
    if (inited_) {
        return;
    }
    NimBLEDevice::init("");
    NimBLEDevice::setPower(ESP_PWR_LVL_P9);
    inited_ = true;
}

void BleSensor::reset_stack() {
    if (inited_) {
        NimBLEDevice::deinit(true);
        inited_ = false;
    }
    begin();
}

bool BleSensor::read(rhcore::Reading& out, uint32_t timeout_ms) {
    begin();
    g_have_reading = false;

    NimBLEClient* c = client();
    // Адрес датчика публичный, тип указывается явно: с неверным типом
    // подключение не проходит вовсе.
    const NimBLEAddress address(SENSOR_MAC, BLE_ADDR_PUBLIC);
    if (!c->connect(address)) {
        return false;
    }

    bool ok = false;
    NimBLERemoteService* svc = c->getService(kService);
    if (svc != nullptr) {
        NimBLERemoteCharacteristic* chr = svc->getCharacteristic(kCharacteristic);
        if (chr != nullptr && chr->canNotify()) {
            // Датчик начинает присылать уведомления сразу после подписки,
            // но интервал у него свой, в единицы секунд, поэтому ждём с
            // запасом и не считаем задержку ошибкой.
            if (chr->subscribe(true, on_notify)) {
                const uint32_t deadline = millis() + timeout_ms;
                while (!g_have_reading && static_cast<int32_t>(deadline - millis()) > 0) {
                    delay(20);
                }
                chr->unsubscribe();
                if (g_have_reading) {
                    out = g_reading;
                    ok = true;
                }
            }
        }
    }
    c->disconnect();
    // Дать стеку закрыть соединение до следующего опроса.
    delay(100);
    return ok;
}
