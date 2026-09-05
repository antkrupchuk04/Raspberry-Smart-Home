import os

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

temperature = None
pressure = None


def save_reading():
    global temperature, pressure

    if temperature is None or pressure is None:
        return

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sensor_readings
                    (device_id, sensor_type, temperature, pressure)
                VALUES (%s, %s, %s, %s)
                """,
                (DEVICE_ID, SENSOR_TYPE, temperature, pressure)
            )

    print(
        f"Saved: {DEVICE_ID} | "
        f"temperature={temperature:.2f} C | "
        f"pressure={pressure:.2f} hPa"
    )

    temperature = None
    pressure = None


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected to MQTT broker")
        client.subscribe("home/climate/room1/#")
        print("Subscribed to home/climate/room1/#")
    else:
        print(f"MQTT connection failed: {reason_code}")


def on_message(client, userdata, msg):
    global temperature, pressure

    try:
        value = float(msg.payload.decode())

        if msg.topic == TOPIC_TEMP:
            temperature = value

        elif msg.topic == TOPIC_PRESSURE:
            pressure = value

        save_reading()

    except ValueError:
        print(f"Invalid value received: {msg.payload!r}")


client = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id="rpi-database-collector"
)

client.on_connect = on_connect
client.on_message = on_message

print("Connecting to MQTT broker...")
client.connect(MQTT_BROKER, MQTT_PORT, 60)

client.loop_forever()
