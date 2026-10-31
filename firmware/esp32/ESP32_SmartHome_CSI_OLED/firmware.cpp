#include <Arduino.h>
#include "firmware.h"
#include "esp_arduino_version.h"

#if ESP_ARDUINO_VERSION_MAJOR != 2 || ESP_ARDUINO_VERSION_MINOR != 0 || ESP_ARDUINO_VERSION_PATCH != 17
#error Select esp32 by Espressif Systems version 2.0.17 in Boards Manager.
#endif
#include <WiFi.h>
#include <Wire.h>
#include <Adafruit_BMP280.h>
#include <DHT.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <PubSubClient.h>
#include <atomic>
#include "esp_wifi.h"
#include "esp_timer.h"
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"
#include "config.h"
#include "secrets.h"
#include "detector.h"

#if !CONFIG_IDF_TARGET_ESP32
#error This project targets the classic ESP32 only, not C3/S3/C6.
#endif
#if !CONFIG_ESP32_WIFI_CSI_ENABLED
#error CSI is disabled in this framework build. Use the official Arduino-ESP32 2.0.17 board package.
#endif

WiFiClient network;
PubSubClient mqtt(network);
Adafruit_BMP280 bmp;
DHT dht(DHT_PIN, DHT11);
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, OLED_RESET);
bool displayReady = false;
float temperature = NAN, pressure = NAN, humidity = NAN;
QueueHandle_t frames, results;
std::atomic<uint32_t> generation{0}, dropped{0}, accepted{0}, rejected{0}, pingOK{0};
portMUX_TYPE filterMux = portMUX_INITIALIZER_UNLOCKED;
uint8_t apMac[6]{};
bool capture = false;
esp_ping_handle_t pingHandle = nullptr;
bool linkUp = false, csiUp = false, bmpPresent = false;
uint32_t lastWifiTry = 0, lastMqttTry = 0, lastSensor = 0, lastReport = 0;
Result latest;
const String statusTopic = "home/status/" DEVICE_ID;
const String csiTopic = "home/motion/" ROOM "/csi";
const String cmdTopic = csiTopic + "/calibrate";
const String climatePrefix = "home/climate/" ROOM "/";

void csiCallback(void *, wifi_csi_info_t *info) {
    if (!info || !info->buf) return;
    bool match; uint32_t gen;
    portENTER_CRITICAL(&filterMux);
    match = capture && memcmp(info->mac, apMac, 6) == 0;
    gen = generation.load();
    portEXIT_CRITICAL(&filterMux);
    if (!match) return;
    // Fixed LLTF layout: HT packets, 20 MHz, no secondary channel.
    // Do not combine legacy/HT40 samples or training fields.
    if (info->len != 128 || info->rx_ctrl.sig_mode != 1 ||
        info->rx_ctrl.cwb != 0 || info->rx_ctrl.secondary_channel != 0 ||
        info->rx_ctrl.rx_state != 0) {
        ++rejected; return;
    }
    Frame f{};
    f.ms = uint32_t(esp_timer_get_time() / 1000);
    f.generation = gen; f.mode = info->rx_ctrl.sig_mode;
    f.channel = info->rx_ctrl.channel;
    memcpy(f.raw, info->buf, sizeof(f.raw));
    // First four bytes may be invalid; Detector never uses those bins.
    if (xQueueSend(frames, &f, 0) != pdTRUE) ++dropped;
    else ++accepted;
}

void processingTask(void *) {
    Detector detector;
    uint32_t gen = generation.load(), lastFrame = millis();
    detector.reset(lastFrame);
    uint32_t previousDrops = dropped.load();
    for (;;) {
        Frame f;
        const uint32_t now = millis();
        if (generation.load() != gen) {
            gen = generation.load(); detector.reset(now);
            Result empty; empty.ms = now; empty.generation = gen;
            xQueueOverwrite(results, &empty);
        }
        // Lost queued frames invalidate the current window.
        if (dropped.load() != previousDrops) {
            previousDrops = dropped.load();
            detector.noData(now);
        }
        if (xQueueReceive(frames, &f, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (f.generation != gen || uint32_t(millis() - f.ms) > 500) continue;
            lastFrame = f.ms;
            detector.add(f);
            Result r = detector.status(); r.generation = gen;
            xQueueOverwrite(results, &r);
        } else if (uint32_t(millis() - lastFrame) > CSI_MAX_GAP_MS) {
            detector.noData(millis());
            Result r = detector.status(); r.generation = gen;
            xQueueOverwrite(results, &r);
        }
    }
}

void stopCapture() {
    portENTER_CRITICAL(&filterMux);
    capture = false; ++generation;
    portEXIT_CRITICAL(&filterMux);
    esp_wifi_set_csi(false); csiUp = false;
    if (pingHandle) {
        esp_ping_stop(pingHandle);
        esp_ping_delete_session(pingHandle);
        pingHandle = nullptr;
    }
    latest = Result{}; latest.generation = generation.load();
}

bool startCapture() {
    wifi_ap_record_t ap{};
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) return false;
    wifi_csi_config_t config{};
    config.lltf_en = true;
    config.htltf_en = false;
    config.stbc_htltf2_en = false;
    config.ltf_merge_en = false;
    config.channel_filter_en = false;
    config.manu_scale = false;
    config.shift = 0;
    esp_err_t err = esp_wifi_set_csi_config(&config);
    if (err == ESP_OK) err = esp_wifi_set_csi_rx_cb(csiCallback, nullptr);
    if (err == ESP_OK) err = esp_wifi_set_csi(true);
    if (err != ESP_OK) {
        Serial.printf("CSI init failed: %s\n", esp_err_to_name(err)); return false;
    }
    esp_ping_config_t pc = ESP_PING_DEFAULT_CONFIG();
    const IPAddress gateway = WiFi.gatewayIP();
    IP_ADDR4(&pc.target_addr, gateway[0], gateway[1], gateway[2], gateway[3]);
    pc.count = ESP_PING_COUNT_INFINITE;
    pc.interval_ms = PING_INTERVAL_MS;
    pc.timeout_ms = 100;
    pc.data_size = 64;
    esp_ping_callbacks_t cb{};
    cb.on_ping_success = [](esp_ping_handle_t, void *) { ++pingOK; };
    err = esp_ping_new_session(&pc, &cb, &pingHandle);
    if (err == ESP_OK) err = esp_ping_start(pingHandle);
    if (err != ESP_OK) {
        Serial.printf("Ping init failed: %s\n", esp_err_to_name(err));
        stopCapture(); return false;
    }
    portENTER_CRITICAL(&filterMux);
    memcpy(apMac, ap.bssid, 6); ++generation; capture = true;
    portEXIT_CRITICAL(&filterMux);
    csiUp = true;
    Serial.printf("CSI started; gateway=%s channel=%u. Keep room still.\n",
                  gateway.toString().c_str(), ap.primary);
    return true;
}

void mqttCallback(char *topic, byte *payload, unsigned length) {
    if (strcmp(topic, cmdTopic.c_str()) == 0 && length == 1 && payload[0] == '1') {
        ++generation;
        latest = Result{}; latest.generation = generation.load();
        Serial.println("Calibration requested; keep room still.");
    }
}

void publishNumber(const char *name, float value) {
    if (!std::isfinite(value)) return;
    char buf[24]; snprintf(buf, sizeof(buf), "%.2f", value);
    mqtt.publish((climatePrefix + name).c_str(), buf, false);
}

void sensors() {
    // Retry BMP280 after a missing sensor, without stopping CSI/Wi-Fi.
    if (!bmpPresent) bmpPresent = bmp.begin(0x76) || bmp.begin(0x77);
    temperature = pressure = NAN;
    if (bmpPresent) {
        temperature = bmp.readTemperature();
        pressure = bmp.readPressure() / 100.0f;
        if (!std::isfinite(temperature) || !std::isfinite(pressure) ||
            pressure < 300 || pressure > 1200) {
            bmpPresent = false; temperature = pressure = NAN;
        }
    }
    humidity = dht.readHumidity();
    const bool dhtOK = std::isfinite(humidity) && humidity >= 0 && humidity <= 100;
    if (!dhtOK) humidity = NAN;
    Serial.printf("Climate: T=%.2f C P=%.2f hPa H=%.1f %%\n",
                  temperature, pressure, humidity);
    if (mqtt.connected()) {
        publishNumber("temperature", temperature);
        publishNumber("pressure", pressure);
        publishNumber("humidity", humidity);
        char health[100];
        snprintf(health, sizeof(health), "{\"bmp280_ok\":%s,\"dht11_ok\":%s}",
                 bmpPresent ? "true" : "false", dhtOK ? "true" : "false");
        mqtt.publish((climatePrefix + "health").c_str(), health, false);
    }
}

void report() {
    Result r;
    if (xQueueReceive(results, &r, 0) == pdTRUE &&
        r.generation == generation.load()) latest = r;
    const bool fresh = csiUp && latest.generation == generation.load() &&
                       uint32_t(millis() - latest.ms) <= 1500;
    const char *state = !fresh ? "no_data" :
        latest.state == Result::READY ? "ready" :
        latest.state == Result::SETTLING ? "settling" :
        latest.state == Result::CALIBRATING ? "calibrating" : "no_data";
    const bool valid = fresh && latest.state == Result::READY;
    char score[24] = "null";
    if (fresh && (latest.state == Result::READY || latest.state == Result::CALIBRATING))
        snprintf(score, sizeof(score), "%.5f", latest.score);
    char payload[512];
    snprintf(payload, sizeof(payload),
        "{\"device_id\":\"%s\",\"state\":\"%s\",\"motion\":%s,\"score\":%s,"
        "\"threshold_on\":%.5f,\"threshold_off\":%.5f,\"calibration\":%u,"
        "\"samples\":%u,\"accepted\":%lu,\"rejected\":%lu,\"dropped\":%lu,"
        "\"ping_replies\":%lu,\"rssi\":%d,\"uptime_ms\":%lu}",
        DEVICE_ID, state, valid ? (latest.motion ? "true" : "false") : "null",
        score, latest.on, latest.off, latest.calibrated, latest.samples,
        (unsigned long)accepted.load(), (unsigned long)rejected.load(),
        (unsigned long)dropped.load(), (unsigned long)pingOK.load(),
        WiFi.RSSI(), (unsigned long)millis());
    Serial.println(payload);
    // Not retained: consumers must expire state if updates stop.
    if (mqtt.connected()) mqtt.publish(csiTopic.c_str(), payload, false);
}

// OLED stays in the Arduino loop task; CSI worker never accesses Wire.
const char *csiDisplayState() {
    if (!csiUp || latest.generation != generation.load() ||
        uint32_t(millis() - latest.ms) > 1500) return "NO DATA";
    switch (latest.state) {
        case Result::SETTLING: return "WAIT";
        case Result::CALIBRATING: return "CAL";
        case Result::READY: return latest.motion ? "MOTION" : "QUIET";
        default: return "NO DATA";
    }
}

void showMessage(const char *line1, const char *line2) {
    if (!displayReady) return;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE); display.setTextSize(1);
    display.setCursor(0, 4); display.println(line1);
    display.setCursor(0, 26); display.println(line2);
    display.display();
}

void updateDisplay() {
    if (!displayReady) return;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE); display.setTextSize(1);
    // This two-color panel has a fixed yellow band at rows 0-15.
    // Keep the header in that band and all sensor text below it.
    display.setCursor(0, 4); display.print("ESP32 SMART HOME");
    display.drawLine(0, 14, OLED_WIDTH - 1, 14, SSD1306_WHITE);
    display.setCursor(0, 18); display.print("Temp: ");
    if (!std::isfinite(temperature)) display.print("ERROR");
    else { display.print(temperature, 1); display.print(" C"); }
    display.setCursor(0, 27); display.print("Hum:  ");
    if (!std::isfinite(humidity)) display.print("ERROR");
    else { display.print(humidity, 1); display.print(" %"); }
    display.setCursor(0, 36); display.print("Pres: ");
    if (!std::isfinite(pressure)) display.print("ERROR");
    else { display.print(pressure, 1); display.print(" hPa"); }
    display.setCursor(0, 45); display.print("CSI: ");
    display.print(csiDisplayState());
    if (latest.state == Result::CALIBRATING &&
        latest.generation == generation.load() && csiUp) {
        display.print(" "); display.print(latest.calibrated);
        display.print("/"); display.print(CSI_CALIBRATION_WINDOWS);
    }
    display.drawLine(0, 54, OLED_WIDTH - 1, 54, SSD1306_WHITE);
    display.setCursor(0, 56);
    display.print("WiFi:"); display.print(WiFi.status() == WL_CONNECTED ? "OK" : "--");
    display.print(" MQTT:"); display.print(mqtt.connected() ? "OK" : "--");
    display.display();
}

void firmwareSetup() {
    Serial.begin(115200);
    Wire.begin(I2C_SDA, I2C_SCL);
    Wire.setTimeOut(50);
    // periphBegin=false: preserve the already configured SDA/SCL pins.
    displayReady = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS, true, false);
    if (displayReady) showMessage("ESP32 SMART HOME", "Initializing...");
    else Serial.println("OLED initialization failed; continuing without OLED");
    dht.begin();
    bmpPresent = bmp.begin(0x76) || bmp.begin(0x77);
    Serial.println(bmpPresent ? "BMP280 initialized" : "BMP280 missing; will retry");
    frames = xQueueCreate(32, sizeof(Frame));
    results = xQueueCreate(1, sizeof(Result));
    if (!frames || !results || xTaskCreate(processingTask, "csi-process", 6144,
        nullptr, 1, nullptr) != pdPASS) {
        Serial.println("Fatal: cannot allocate CSI worker");
        while (true) delay(1000);
    }
    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setCallback(mqttCallback);
    mqtt.setBufferSize(768);
    mqtt.setKeepAlive(15);
    mqtt.setSocketTimeout(1);
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    esp_err_t bw = esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);
    if (bw != ESP_OK) Serial.printf("HT20 setup: %s\n", esp_err_to_name(bw));
    WiFi.setSleep(false);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    lastWifiTry = millis();
    sensors(); lastSensor = millis();
    updateDisplay();
    Serial.println("Serial command c: recalibrate. Connect to 2.4 GHz / 20 MHz AP.");
}

void firmwareLoop() {
    const uint32_t now = millis();
    if (Serial.available() && Serial.read() == 'c') {
        ++generation; latest = Result{}; latest.generation = generation.load();
        Serial.println("Calibration requested; keep room still.");
    }
    const bool connected = WiFi.status() == WL_CONNECTED;
    if (!connected) {
        if (linkUp) { stopCapture(); mqtt.disconnect(); linkUp = false; }
        if (uint32_t(now - lastWifiTry) >= 5000) {
            lastWifiTry = now; WiFi.reconnect();
        }
    } else {
        if (!linkUp) {
            linkUp = true; WiFi.setSleep(false); startCapture();
        }
        // Handle roaming/channel changes, even if WL_CONNECTED never drops.
        static uint32_t lastAPCheck = 0;
        if (uint32_t(now - lastAPCheck) >= 1000) {
            lastAPCheck = now;
            wifi_ap_record_t ap{};
            if (csiUp && esp_wifi_sta_get_ap_info(&ap) == ESP_OK &&
                memcmp(ap.bssid, apMac, 6) != 0) {
                stopCapture(); startCapture();
            }
        }
        static uint32_t lastCsiTry = 0;
        if (!csiUp && uint32_t(now - lastCsiTry) > 10000) {
            lastCsiTry = now; startCapture();
        }
        if (!mqtt.connected() && uint32_t(now - lastMqttTry) >= 5000) {
            lastMqttTry = now;
            if (mqtt.connect(DEVICE_ID, MQTT_USER, MQTT_PASSWORD,
                             statusTopic.c_str(), 1, true, "offline")) {
                mqtt.publish(statusTopic.c_str(), "online", true);
                mqtt.subscribe(cmdTopic.c_str());
            }
        }
        if (mqtt.connected()) mqtt.loop();
    }
    if (uint32_t(now - lastSensor) >= SENSOR_INTERVAL_MS) {
        lastSensor = now; sensors();
    }
    if (uint32_t(now - lastReport) >= 1000) {
        lastReport = now; report(); updateDisplay();
    }
    delay(2);
}
