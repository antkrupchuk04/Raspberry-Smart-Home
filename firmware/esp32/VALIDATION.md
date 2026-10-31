# Validation — 2026-10-31

The repository copy was compiled for `esp32:esp32:esp32` using the installed
Arduino-ESP32 2.0.17 toolchain. A temporary build copy used blank credentials
from `secrets.h.example`; no private credentials or build output are committed.

- Full firmware compilation: passed, 779965 bytes flash (59%), 45492 bytes
  static RAM (13%).
- Host detector tests: passed with C++11, `-Wall -Wextra -Werror`, address
  sanitizer and undefined-behavior sanitizer. Covers calibration, motion,
  clearing, no-data, channel change, invalid-prefix immunity and sparse traffic.
- Credential scan: original Wi-Fi SSID/password absent from added files;
  example Wi-Fi/MQTT credentials blank; private `secrets.h` excluded by Git.

Public GPIO, MQTT topics and detector behavior are preserved from the Desktop
source. Credential definitions moved out of `config.h` into a private header
included only by `firmware.cpp`, so host detector tests need no credentials.

The existing uploaded board has published real CSI reports to the Raspberry Pi.
This repository copy has not been flashed as part of this change. Compilation
and synthetic tests do not establish real-world detection accuracy or replace
hardware checks of sensors, OLED, reconnection and calibration.

Run the host tests from this directory:

```sh
c++ -std=c++11 -Wall -Wextra -Werror -I ESP32_SmartHome_CSI_OLED tests/detector_test.cpp -o /tmp/esp32-detector-test
/tmp/esp32-detector-test
```
