# Raspberry Smart Home

An engineering-grade smart-home monitoring platform for a Master's project:

> **Дослідження та реалізація апаратно-програмної системи розумного дому з функцією моніторингу руху на базі Raspberry Pi та ESP32**

The current system records environmental telemetry from an ESP32 and BMP280 sensor, stores it in PostgreSQL, and presents current values and history through a FastAPI dashboard.

## Architecture

```text
BMP280 → ESP32 → MQTT (Mosquitto) → Python collector → PostgreSQL → FastAPI → Dashboard
```

## Current capabilities

- ESP32 temperature, pressure and DHT11 humidity telemetry over MQTT
- PostgreSQL storage for sensor history
- FastAPI REST endpoints and interactive local dashboard
- Temperature/humidity/pressure history chart with 1, 6, and 24-hour ranges, CSV export and fullscreen
- Cached outdoor weather and local AI room analysis
- Responsive monitoring console with reusable cards and dedicated future-module sections
- Online/offline indicator based on reading freshness
- Living room Zigbee PIR motion events, PostgreSQL history, battery and signal diagnostics
- systemd-managed Mosquitto, PostgreSQL, collector, and API services

See [Dashboard architecture](DASHBOARD.md) for the grid layout, reusable components, responsive behavior, polling intervals and module extension guide. [System Status](SYSTEM_STATUS.md) provides real Pi CPU/RAM/disk/temperature/uptime, MQTT, database, Internet and ESP32 health through `GET /api/system/status`.

See [Motion integration](MOTION.md) for the Tuya TS0202, configuration, event counting and verification commands.

## Hardware

- Raspberry Pi 5 (8 GB)
- ESP32-WROOM
- BMP280 temperature and pressure sensor
- Official Raspberry Pi PSU
- Pironman 5 Mini case
- Kingston Canvas Go! Plus 64 GB microSD card

Planned hardware includes an HC-SR501 PIR sensor and an NVMe SSD.

## Project layout

```text
.
├── api.py                 # FastAPI application and REST endpoints
├── mqtt_collector.py      # MQTT-to-PostgreSQL collector
├── static/                # Dashboard HTML, CSS, and JavaScript
├── .env.example           # Configuration template; safe to commit
└── .gitignore
```

## MQTT topics

| Topic | Value |
| --- | --- |
| `home/climate/room1/temperature` | Temperature in °C |
| `home/climate/room1/pressure` | Atmospheric pressure in hPa |
| `home/climate/room1/humidity` | Relative humidity in % |
| `zigbee2mqtt/0xa4c138657fddb903` | Living room PIR occupancy, battery and signal JSON |

The ESP32 normally publishes telemetry about every five seconds.

## Configuration

The running system reads its configuration from `.env` through systemd. Create it from the committed template:

```bash
cd ~/smarthome
cp .env.example .env
chmod 600 .env
```

Set the PostgreSQL and MQTT values for the deployment. Do not commit `.env`; it contains credentials and is ignored by Git.

## API

| Endpoint | Description |
| --- | --- |
| `GET /` | Dashboard |
| `GET /docs` | FastAPI OpenAPI documentation |
| `GET /api/sensors/latest` | Most recent sensor reading |
| `GET /api/sensors/history?hours=1` | Sensor history for 1, 6, or 24 hours |
| `GET /api/system/status` | Cached real host metrics, dependency health and optional Zigbee detection |
| `GET /api/motion?hours=24&limit=20` | Last reported PIR state, today's detection count and recent events |

Example latest-reading response:

```json
{
  "id": 123,
  "device_id": "esp32-room1",
  "sensor_type": "BMP280",
  "temperature": 24.63,
  "pressure": 976.53,
  "humidity": null,
  "recorded_at": "2026-09-05T00:42:15+03:00"
}
```

## Operations

The production services are managed by systemd:

```bash
sudo systemctl status mosquitto postgresql smarthome-collector smarthome-api
sudo systemctl restart smarthome-collector smarthome-api
```

Open the dashboard at `http://<raspberry-pi-ip>:8000/`.

## Roadmap

1. Restore and monitor ESP32 telemetry freshness.
2. Expand motion monitoring to additional rooms and sensors.
3. Add device availability monitoring for Zigbee sensors.
4. Send Telegram notifications for motion events.
5. Research Wi-Fi CSI motion detection.
6. Add Tailscale remote access, backups, and storage monitoring.

## Security

Keep `.env`, SSH private keys, database dumps, and virtual environments out of Git. Use MQTT authentication and topic ACLs before exposing the system beyond the trusted local network.

## AI Room Analytics

`GET /api/analytics/room` provides lightweight local environmental analysis. “AI” here means local statistical analysis and machine learning on the Raspberry Pi; it does not use a cloud service or generative AI model.

- Comfort scoring uses configurable temperature and humidity ranges. Humidity is optional because BMP280 does not provide it.
- An `IsolationForest` checks recent valid readings, temperature/pressure change rates, and humidity when enough humidity data exists. It needs at least 30 valid samples and retrains at most once every five minutes.
- A linear regression calculates the recent temperature trend and a 30-minute forecast when its fit has sufficient quality.

Install the required local dependencies:

```bash
source ~/smarthome/venv/bin/activate
pip install -r requirements.txt
```

Example request:

```bash
curl http://127.0.0.1:8000/api/analytics/room
```

The endpoint reports `available: false` for a subsystem with insufficient fresh history rather than fabricating an analysis.

## Current Outdoor Weather

`GET /api/weather/current` obtains current outdoor weather through the FastAPI backend from Open-Meteo. Set `WEATHER_LAT`, `WEATHER_LON`, and `WEATHER_LOCATION_NAME` in `.env`. The backend caches successful responses for ten minutes; if a refresh fails, it returns the last cached result marked `stale: true`. No API key is required and the browser never calls Open-Meteo directly.
