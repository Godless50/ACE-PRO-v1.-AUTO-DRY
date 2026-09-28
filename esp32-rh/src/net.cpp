#include "net.h"

#include <Arduino.h>
#include <ElegantOTA.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <WebServer.h>
#include <WiFi.h>

#include "config.h"
#include "gcode.h"

namespace {

WiFiClient g_mqtt_socket;
PubSubClient g_mqtt(g_mqtt_socket);
WebServer g_web(80);

char g_last_command[rhcore::kGcodeBufSize] = "";
uint32_t g_silence_ms = 0;
uint8_t g_failures = 0;
uint32_t g_delivered_total = 0;
uint32_t g_failed_total = 0;

String topic(const char* leaf) {
    return String(MQTT_TOPIC_PREFIX) + "/" + leaf;
}

void handle_status() {
    // Страница на случай, когда плата уже в корпусе и последовательный
    // порт недоступен.
    String body = "{";
    body += "\"wifi\":\"" + WiFi.localIP().toString() + "\"";
    body += ",\"rssi\":" + String(WiFi.RSSI());
    body += ",\"mqtt\":" + String(g_mqtt.connected() ? "true" : "false");
    body += ",\"last_command\":\"" + String(g_last_command) + "\"";
    body += ",\"silence_seconds\":" + String(g_silence_ms / 1000);
    body += ",\"consecutive_failures\":" + String(g_failures);
    body += ",\"delivered_total\":" + String(g_delivered_total);
    body += ",\"failed_total\":" + String(g_failed_total);
    body += ",\"uptime_seconds\":" + String(millis() / 1000);
    body += "}";
    g_web.send(200, "application/json", body);
}

}  // namespace

void Net::begin() {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);  // сон Wi-Fi ломает отзывчивость прошивки по воздуху
    WiFi.setAutoReconnect(true);
    WiFi.setHostname(OTA_HOSTNAME);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    g_mqtt.setServer(MQTT_HOST, MQTT_PORT);
    g_mqtt.setBufferSize(512);

    g_web.on("/status", handle_status);
    g_web.on("/", []() {
        g_web.sendHeader("Location", "/update");
        g_web.send(302, "text/plain", "");
    });
    ElegantOTA.begin(&g_web);
    ElegantOTA.setAuth(OTA_HOSTNAME, OTA_PASSWORD);
    g_web.begin();
}

bool Net::wifi_ready() const { return WiFi.status() == WL_CONNECTED; }

void Net::ensure_mqtt() {
    if (!wifi_ready() || g_mqtt.connected()) {
        return;
    }
    const String id = String(OTA_HOSTNAME) + "-" + String(ESP.getEfuseMac(), HEX);
    if (strlen(MQTT_USER) > 0) {
        g_mqtt.connect(id.c_str(), MQTT_USER, MQTT_PASSWORD);
    } else {
        g_mqtt.connect(id.c_str());
    }
}

void Net::loop() {
    g_web.handleClient();
    ElegantOTA.loop();
    ensure_mqtt();
    if (g_mqtt.connected()) {
        g_mqtt.loop();
    }
}

bool Net::send_gcode(const char* script) {
    if (!wifi_ready() || script == nullptr) {
        return false;
    }
    HTTPClient http;
    const String url = String(MOONRAKER_URL) + "/printer/gcode/script";
    if (!http.begin(url)) {
        return false;
    }
    http.addHeader("Content-Type", "application/json");
    // Moonraker держит запрос открытым, пока G-code не выполнится, поэтому
    // таймаут заведомо больше, чем у обычного запроса.
    http.setTimeout(15000);

    String body = "{\"script\":\"";
    body += script;
    body += "\"}";
    const int code = http.POST(body);
    http.end();

    const bool ok = (code == 200);
    if (ok) {
        snprintf(g_last_command, sizeof(g_last_command), "%s", script);
        ++g_delivered_total;
    } else {
        ++g_failed_total;
    }
    return ok;
}

void Net::publish_reading(const rhcore::Reading& r, bool delivered) {
    if (!g_mqtt.connected()) {
        return;
    }
    // Влажность целая, температура в сотых долях — печатаем из целых, чтобы
    // значение в MQTT совпадало с отправленным на принтер.
    g_mqtt.publish(topic("humidity").c_str(), String(r.rh_percent).c_str(), true);
    char temp[16];
    snprintf(temp, sizeof(temp), "%d.%02d", r.temp_centi_c / 100,
             abs(r.temp_centi_c % 100));
    g_mqtt.publish(topic("temperature").c_str(), temp, true);
    g_mqtt.publish(topic("battery_mv").c_str(), String(r.battery_mv).c_str(), true);
    g_mqtt.publish(topic("delivered").c_str(), delivered ? "1" : "0", true);
    g_mqtt.publish(topic("silence_seconds").c_str(),
                   String(g_silence_ms / 1000).c_str(), true);
    g_mqtt.publish(topic("failures").c_str(), String(g_failures).c_str(), true);
}

void Net::publish_event(const char* name, const char* detail) {
    if (!g_mqtt.connected()) {
        return;
    }
    String payload = String("{\"event\":\"") + name + "\",\"detail\":\"" +
                     (detail ? detail : "") + "\",\"uptime\":" +
                     String(millis() / 1000) + "}";
    g_mqtt.publish(topic("event").c_str(), payload.c_str(), false);
}

void Net::set_status(const char* last_command, uint32_t silence_ms,
                     uint8_t failures) {
    if (last_command != nullptr) {
        snprintf(g_last_command, sizeof(g_last_command), "%s", last_command);
    }
    g_silence_ms = silence_ms;
    g_failures = failures;
}
