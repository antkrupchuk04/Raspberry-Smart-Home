#pragma once
// Public hardware/network settings. Credentials belong in ignored secrets.h.
// Classic ESP32-WROOM / ESP32 Dev Module only.
#define MQTT_HOST "192.168.0.110" // Raspberry Pi address, not router
#define MQTT_PORT 1883
#define DEVICE_ID "esp32-room1"
#define ROOM "room1"
#define I2C_SDA 4
#define I2C_SCL 15
#define DHT_PIN 21
#define SENSOR_INTERVAL_MS 5000u
#define PING_INTERVAL_MS 20u
#define CSI_WINDOW_MS 1000u
#define CSI_MIN_PACKETS 20u
#define CSI_MAX_GAP_MS 200u
#define CSI_CALIBRATION_WINDOWS 30
#define CSI_SETTLE_MS 10000u
#define CSI_MIN_MARGIN 0.015f
#define CSI_MAD_MULTIPLIER 6.0f
#define CSI_ON_WINDOWS 2
#define CSI_OFF_WINDOWS 3

// OLED SSD1306 on the same I2C bus as BMP280.
#define OLED_WIDTH 128
#define OLED_HEIGHT 64
#define OLED_ADDRESS 0x3C
#define OLED_RESET -1
