import os
import logging
import threading

import paho.mqtt.client as mqtt
import psycopg2

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "database": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

DEVICE_ID = "esp32-room1"
SENSOR_TYPE = "BMP280"

TOPIC_TEMP = "home/climate/room1/temperature"
TOPIC_PRESSURE = "home/climate/room1/pressure"
TOPIC_HUMIDITY = "home/climate/room1/humidity"

# The ESP32 publishes the three measurements as separate MQTT messages. Wait
# briefly so one database row represents one complete measurement cycle.
BATCH_WINDOW_SECONDS = 0.75

temperature = None
pressure = None
humidity = None
save_timer = None
state_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)


def save_reading():
    global temperature, pressure, humidity, save_timer

    with state_lock:
        if temperature is None or pressure is None:
            return
        reading_temperature = temperature
        reading_pressure = pressure
        reading_humidity = humidity
        temperature = None
        pressure = None
        humidity = None
        save_timer = None

    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sensor_readings
                        (device_id, sensor_type, temperature, pressure, humidity)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (DEVICE_ID, SENSOR_TYPE, reading_temperature, reading_pressure, reading_humidity)
                )
    except psycopg2.Error:
        LOGGER.exception("Failed to store sensor reading")
        return

    humidity_text = "unavailable" if reading_humidity is None else f"{reading_humidity:.1f} %"
    LOGGER.info(
        "Saved %s: temperature=%.2f C | pressure=%.2f hPa | humidity=%s",
        DEVICE_ID,
        reading_temperature,
        reading_pressure,
        humidity_text,
    )


def schedule_save():
    global save_timer

    with state_lock:
        if temperature is None or pressure is None:
            return
        if save_timer is not None:
            save_timer.cancel()
        save_timer = threading.Timer(BATCH_WINDOW_SECONDS, save_reading)
        save_timer.daemon = True
        save_timer.start()


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected to MQTT broker")
        client.subscribe("home/climate/room1/#")
        print("Subscribed to home/climate/room1/#")
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, msg):
    global temperature, pressure, humidity

    try:
        value = float(msg.payload.decode())

        with state_lock:
            if msg.topic == TOPIC_TEMP:
                temperature = value
            elif msg.topic == TOPIC_PRESSURE:
                pressure = value
            elif msg.topic == TOPIC_HUMIDITY:
                humidity = value
            else:
                return

        schedule_save()

    except ValueError:
        LOGGER.warning("Invalid value received on %s: %r", msg.topic, msg.payload)


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="rpi-database-collector"
)

client.on_connect = on_connect
client.on_message = on_message

print("Connecting to MQTT broker...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

client.loop_forever()
