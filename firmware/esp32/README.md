# ESP32 Room 1 firmware

Source for the classic ESP32-WROOM / ESP32 Dev Module currently publishing
BMP280 temperature/pressure, DHT11 humidity and Wi-Fi CSI motion diagnostics.
SSD1306 OLED shows climate values and the device's raw CSI state.

## Configure and build

1. Open `ESP32_SmartHome_CSI_OLED/ESP32_SmartHome_CSI_OLED.ino` in Arduino IDE.
2. Copy `ESP32_SmartHome_CSI_OLED/secrets.h.example` to `secrets.h` in the same
   directory, then fill in Wi-Fi credentials and optional MQTT authentication.
   The private file is ignored by Git. Keep the committed example blank.
3. Review public `config.h`: MQTT broker address/port, device/room ID and GPIOs.
4. Select **ESP32 Dev Module**, with **esp32 by Espressif Systems 2.0.17**.
   The source intentionally requires this board package for its CSI API.
5. Install PubSubClient 2.8, Adafruit BMP280 Library 3.0.0, DHT sensor library
   1.4.7, Adafruit GFX Library 1.12.6 and Adafruit SSD1306 2.5.17, including
   Adafruit BusIO and Unified Sensor dependencies.
6. Verify, then upload only when ready. Serial Monitor uses 115200 baud.

Equivalent Arduino CLI compilation from this directory after configuring secrets:

```sh
arduino-cli compile --fqbn esp32:esp32:esp32 ESP32_SmartHome_CSI_OLED
```

Credentials are compiled into the firmware binary; treat generated binaries as
private too. Build artifacts are ignored. No credentials are copied from the
original Desktop sketch into this repository.

## Hardware and telemetry

- Shared BMP280/OLED I2C: SDA GPIO4, SCL GPIO15; DHT11 DATA GPIO21.
- BMP280 addresses 0x76/0x77; SSD1306 128x64 at 0x3C.
- Climate: `home/climate/room1/{temperature,pressure,humidity}` every 5 seconds.
- CSI: `home/motion/room1/csi` every second, non-retained, QoS 0.
- Availability: retained `home/status/esp32-room1` with offline LWT.
- Recalibration: non-retained `1` to `home/motion/room1/csi/calibrate`, or `c`
  in Serial Monitor. Keep the room still during settling/calibration.

CSI uses real radio channel measurements, not an RSSI-only motion heuristic.
It requires 2.4 GHz HT20 traffic from the associated access point. Detection is
experimental and depends on placement, calibration and radio conditions.
`no_data`/calibration have unknown motion, not a cleared state.

The dashboard applies an additional server-side confirmation filter and refreshes
every five seconds; its filtered state may differ from the OLED's raw state.
Confirmed transitions are stored separately from Zigbee PIR events. CSI does not
send Telegram alerts. See the repository's `MOTION.md` for backend behavior.

## Detector tests (no credentials or board required)

Run from this directory:

```sh
c++ -std=c++11 -Wall -Wextra -Werror -I ESP32_SmartHome_CSI_OLED tests/detector_test.cpp -o /tmp/esp32-detector-test
/tmp/esp32-detector-test
```

See `README_UA.md` for detailed CSI design and diagnostics, and `VALIDATION.md`
for verification limits. Adding this source does not flash or restart the board.
