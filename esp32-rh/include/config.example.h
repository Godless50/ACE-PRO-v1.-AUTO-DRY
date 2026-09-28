#pragma once
// Скопировать в include/config.h и заполнить. Файл config.h в git не
// попадает: в нём пароли.
//
//   cp include/config.example.h include/config.h

// --- Wi-Fi -----------------------------------------------------------------
#define WIFI_SSID "ИМЯ_СЕТИ"
#define WIFI_PASSWORD "ПАРОЛЬ_СЕТИ"
// Имя платы в сети: под ним она видна для прошивки по воздуху.
#define OTA_HOSTNAME "ace-rh"
#define OTA_PASSWORD "ПАРОЛЬ_ДЛЯ_ПРОШИВКИ_ПО_ВОЗДУХУ"

// --- Датчик ----------------------------------------------------------------
// Адрес Xiaomi LYWSD03MMC. Проверен живым чтением.
#define SENSOR_MAC "AA:BB:CC:DD:EE:FF"

// --- Принтер ---------------------------------------------------------------
// Moonraker без авторизации, ключ не нужен.
#define MOONRAKER_URL "http://<printer-ip>:7125"
// Индекс ACE в multiACE, нумерация с нуля.
#define ACE_INDEX 0
// Срок годности показания в секундах. По истечении модуль ace_ext_rh.py на
// принтере ГАСИТ нагрев. Менять осознанно: это предохранитель.
#define RH_TTL_SECONDS 1800
// Период опроса датчика в секундах.
#define INTERVAL_SECONDS 300

// --- MQTT для графиков и панели в Home Assistant ---------------------------
#define MQTT_HOST "<server-ip>"
#define MQTT_PORT 1883
#define MQTT_USER ""      // пусто — без авторизации
#define MQTT_PASSWORD ""
#define MQTT_TOPIC_PREFIX "ace/rh"
