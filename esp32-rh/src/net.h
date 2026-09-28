#pragma once
#include <stdint.h>

#include "reading.h"

// Сеть: Wi-Fi, отправка G-code в Moonraker, телеметрия в MQTT, обновление
// прошивки по воздуху и страница состояния.
class Net {
public:
    void begin();

    // Обслуживание фоновых дел: переподключение Wi-Fi и MQTT, приём
    // запросов веб-сервера и обновлений по воздуху. Вызывать часто.
    void loop();

    bool wifi_ready() const;

    // Отправить команду в Moonraker. true — принтер ответил успехом.
    bool send_gcode(const char* script);

    // Опубликовать показание и счётчики. Ошибки публикации не считаются
    // значимыми: телеметрия не должна влиять на работу автосушки.
    void publish_reading(const rhcore::Reading& r, bool delivered);
    void publish_event(const char* name, const char* detail);

    // Данные для страницы /status и для телеметрии.
    void set_status(const char* last_command, uint32_t silence_ms,
                    uint8_t failures);

private:
    void ensure_mqtt();
};
