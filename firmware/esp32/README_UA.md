# BMP280 + DHT11 + OLED + Wi-Fi CSI

Інтегровано CSI у надану вами конфігурацію. Класичний ESP32 Dev Module / ESP32-WROOM.
Публічні налаштування містяться у `config.h`. Скопіюйте `secrets.h.example`
у `secrets.h` та вкажіть власні Wi-Fi/MQTT облікові дані. `secrets.h` ігнорується Git;
реальних SSID і паролів у репозиторії немає. Вихідні MQTT-теми клімату збережено.

## Запуск в Arduino IDE

1. Розпакуйте весь архів. Відкрийте
   `ESP32_SmartHome_CSI_OLED/ESP32_SmartHome_CSI_OLED.ino`.
   Файли .ino, .cpp та .h повинні бути разом у цій папці.
2. Boards Manager → **esp32 by Espressif Systems → 2.0.17**.
   Це версія пакета плат, а не Arduino IDE. Виберіть **ESP32 Dev Module**.
   Якщо пакет відсутній, у Additional Boards Manager URLs додайте:
   https://espressif.github.io/arduino-esp32/package_esp32_index.json
3. Library Manager: **PubSubClient 2.8**, **Adafruit BMP280 Library 3.0.0**,
   **DHT sensor library 1.4.7**, **Adafruit GFX Library**, **Adafruit SSD1306**.
   Погодьтеся на встановлення залежностей Adafruit BusIO / Unified Sensor.
4. Створіть `secrets.h` з прикладу, заповніть облікові дані та звірте `config.h`.
   Виберіть COM/USB-порт → Verify → Upload.
   Serial Monitor: **115200 baud**. Не встановлюйте окрему ESP32Ping-бібліотеку:
   використовується API ping з пакета ESP32.
5. Залиште кімнату без руху приблизно на 45–60 секунд, доки CSI не стане QUIET.
   Потім пройдіться кімнатою і перевірте score та MOTION.

## Збережена конфігурація

| Параметр | Значення |
|---|---|
| Спільний I2C SDA: BMP280 + OLED | GPIO4 |
| Спільний I2C SCL: BMP280 + OLED | GPIO15 |
| DHT11 DATA | GPIO21 |
| OLED | SSD1306 128×64, 0x3C |
| BMP280 | спочатку 0x76, потім резервно 0x77 |
| MQTT | 192.168.0.110:1883, клієнт esp32-room1 |
| Клімат | раз на 5 секунд |

OLED показує температуру, вологість, тиск, CSI та статуси Wi-Fi/MQTT.
Додатковий стан CSI: WAIT (стабілізація), CAL n/30 (калібрування),
QUIET (нижче порогів), MOTION (рух), NO DATA (немає достатніх даних).

Роутер: **2.4 GHz, 802.11n, ширина каналу 20 MHz**, ICMP echo до шлюзу дозволено.
Фіксований канал та нерухомі роутер/ESP32 спрощують експеримент.

## Принцип CSI

ESP32 пінгує DHCP-шлюз кожні 20 мс (бажано близько 50 відповідей/с).
CSI оцінюється під час приймання кадрів від поточного AP: роутер не надсилає
готові CSI-дані у ping. Callback копіює дані в чергу без очікування.
Окрема FreeRTOS-задача виконує аналіз, не торкаючись OLED, Wire або MQTT.

Навмисно приймаються тільки HT20/LLTF одного формату: 128 байт, sig_mode=1,
cwb=0, secondary_channel=0, rx_state=0. Інші кадри відкидаються.
Вибрано 42 піднесучі +6..+26 та -26..-6; DC, крайові і перші 4 потенційно
некоректні байти не використовуються. Формат signed int8: imaginary, real.

Амплітуди нормалізуються на середню амплітуду кадру. Для секундного вікна
обчислюється std/mean на кожній піднесучій, score — їх медіана.
Це послаблює загальні зміни підсилення, але може послабити і частину руху.
Потрібно >=20 придатних кадрів/вікно без розриву >200 мс.
Пропуски/переповнення черги скидають поточне вікно. За відсутності даних
motion=null: це не відсутність людини.

Калібрування: 10 секунд стабілізації + 30 придатних секундних вікон.
Поріг увімкнення = медіана фону + max(6×MAD, 0.015); поріг вимкнення нижчий.
Рух підтверджують 2 високі вікна поспіль, завершення — 3 низькі вікна.
Затримка кілька секунд очікувана. score не є ймовірністю або відсотком точності.
Не рухайтеся під час навчання; алгоритм сам не перевіряє, чи кімната порожня.
Калібрування в RAM, повторюється після перезавантаження/перепідключення,
зміни точки доступу або каналу.

## MQTT

| Topic | Дані |
|---|---|
| `home/climate/room1/temperature` | число, °C з BMP280 |
| `home/climate/room1/pressure` | число, hPa з BMP280 |
| `home/climate/room1/humidity` | число, % з DHT11 |
| `home/climate/room1/health` | JSON доступності датчиків |
| `home/motion/room1/csi` | JSON CSI щосекунди |
| `home/motion/room1/csi/calibrate` | команда: `1`, без retain |
| `home/status/esp32-room1` | online/offline; retained, MQTT LWT |

Приклад CSI, значення ілюстративні:

```json
{"device_id":"esp32-room1","state":"ready","motion":true,"score":0.09,
 "threshold_on":0.05,"threshold_off":0.04,"calibration":30,"samples":51,
 "accepted":5000,"rejected":100,"dropped":0,"ping_replies":5100,
 "rssi":-55,"uptime_ms":120000}
```

Стани JSON: settling, calibrating, ready, no_data.
Лише при ready поле motion має true/false, в інших станах — null.
Дані CSI публікуються без retain і з QoS0; черги подій за час відсутності MQTT немає.
Лічильники накопичуються від завантаження, samples — кількість кадрів останнього
завершеного придатного вікна. uptime_ms — не календарний час.
Сервер повинен вважати CSI невідомим при відсутності оновлень >5 секунд.
Для Telegram відстежуйте перехід motion=false→true, а не кожне true-повідомлення.
У LWT є затримка до MQTT keepalive timeout.

На Raspberry:

```sh
mosquitto_sub -h localhost -t 'home/motion/room1/#' -v
mosquitto_pub -h localhost -t home/motion/room1/csi/calibrate -m 1
```

Альтернатива калібруванню через MQTT — літера **c** у Serial Monitor.
Не встановлюйте retain для команди калібрування.
Програма не змінює backend, PostgreSQL або Telegram-бота автоматично.

## Діагностика

- `ping_replies` не зростає: перевірте з'єднання та ICMP на шлюзі.
- Відповіді ping є, accepted=0/rejected росте: перевірте HT20/802.11n і 20 MHz.
  Деякі роутери надсилають відповіді іншим PHY-форматом: цей парсер його не
  приймає. Не видаляйте фільтри без правильного парсера відповідного формату.
- `no_data`: замало придатних кадрів, пропуски, слабкий сигнал або dropped.
- Хибні спрацювання: повторіть калібрування в нерухомій кімнаті; перевірте фон.
- Рух не видно: змініть розташування; порівняйте score у спокої та під час ходьби.
  Лише потім налаштовуйте CSI_MIN_MARGIN/CSI_MAD_MULTIPLIER у config.h.

BMP280, DHT11 та OLED обслуговуються головною задачею. Помилка BMP280 тепер
не зупиняє всю програму: CSI продовжує працювати, датчик перевіряється повторно.
Невалідні показники відображаються як ERROR і не публікуються як свіжі числа.
MQTT connect може затримувати головний цикл, CSI-обробка залишається окремою.
DHT11 короткочасно блокує переривання; на реальній платі перевіряйте dropped.

Це експериментальний детектор змін радіоканалу, а не класифікатор людей:
рух тварин/предметів і радіозавади також можуть впливати на результат.
Нерухома присутність не гарантується. Реальні пороги та точність потрібно
перевірити у вашому приміщенні. Стан перевірок — у VALIDATION.md.

## Документація API

- https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32/api-guides/wifi.html#wi-fi-channel-state-information
- https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32/api-reference/protocols/icmp_echo.html
- https://docs.espressif.com/projects/arduino-esp32/en/latest/installing.html
