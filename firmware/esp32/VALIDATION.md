# Validation — 2026-10-31

The repository copy was compiled for `esp32:esp32:esp32` using the installed
Arduino-ESP32 2.0.17 toolchain. A temporary build copy used blank credentials
from `secrets.h.example`; no private credentials or build output are committed.

- Full firmware compilation: passed, 779961 bytes flash (59%), 45492 bytes
  static RAM (13%).
- Host detector tests: passed with C++11, `-Wall -Wextra -Werror`, address
  sanitizer and undefined-behavior sanitizer. Covers calibration, motion,
  clearing, no-data, channel change, invalid-prefix immunity and sparse traffic.
- Credential scan: original Wi-Fi SSID/password absent from added files;
  example Wi-Fi/MQTT credentials blank; private `secrets.h` excluded by Git.

The OLED header stays within the fixed top 16-row yellow band; temperature,
humidity, pressure and CSI text start at rows 18, 27, 36 and 45 in the blue band.
A framebuffer preview using the installed Adafruit font confirmed no text
clipping or overlaps.

Public GPIO, MQTT topics and detector behavior are preserved from the Desktop
source. Credential definitions moved out of `config.h` into a private header
included only by `firmware.cpp`, so host detector tests need no credentials.

The existing uploaded board has published real CSI reports to the Raspberry Pi.
The corrected configured Desktop sketch was compiled separately (780001 bytes
flash, 45492 bytes static RAM) and uploaded to the connected ESP32-D0WD-V3 on
31 October 2026. The upload verified flash hashes and reset the board. Private
Wi-Fi settings were retained locally and are absent from the repository.
The framebuffer preview verifies layout; physical screen appearance still
requires visual confirmation. Compilation and synthetic tests do not establish
real-world detection accuracy.

Run the host tests from this directory:

```sh
c++ -std=c++11 -Wall -Wextra -Werror -I ESP32_SmartHome_CSI_OLED tests/detector_test.cpp -o /tmp/esp32-detector-test
/tmp/esp32-detector-test
```
