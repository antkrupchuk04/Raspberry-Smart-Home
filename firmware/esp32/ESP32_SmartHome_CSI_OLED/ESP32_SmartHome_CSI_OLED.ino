// Open this file in Arduino IDE; keep all files in this folder together.
// Classic ESP32 Dev Module; Arduino-ESP32 board package 2.0.17.
// Set Wi-Fi credentials in secrets.h; MQTT host and GPIOs in config.h.
#include "firmware.h"

void setup() { firmwareSetup(); }
void loop() { firmwareLoop(); }
