# Zigbee motion integration

The existing MQTT collector receives real PIR reports from Zigbee2MQTT, persists them in PostgreSQL, and serves the Motion card through `GET /api/motion`. The dashboard refreshes motion every five seconds without changing other polling intervals. The sensor is a Tuya TS0202 in the Living room.

## Configuration

Add these deployment settings to the existing private `.env`:

```dotenv
MOTION_SENSOR_TOPIC=zigbee2mqtt/0xa4c138657fddb903
MOTION_SENSOR_ID=0xa4c138657fddb903
MOTION_SENSOR_NAME="Tuya TS0202"
MOTION_SENSOR_ROOM="Living room"
ZIGBEE_SERIAL_DEVICE=/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

Use an exact device topic, without MQTT wildcards. Update it if the Zigbee2MQTT friendly name changes; retain the stable IEEE address as `MOTION_SENSOR_ID`. An empty topic disables motion collection. Database and broker credentials/configuration are shared with the existing application. No new runtime dependencies are needed.

Initialize the additive schema once, then restart only the application services:

```sh
cd ~/smarthome
set -a
. ./.env
set +a
venv/bin/python motion_service.py --init-db
sudo systemctl restart smarthome-collector smarthome-api
```

The migration creates `motion_sensor_state`, `motion_events` and an index. It is safe to repeat and does not modify existing climate data. PostgreSQL itself does not need restarting.

## Event semantics

- `occupancy: true` means the PIR detected motion; `false` is a clear report. It is not continuous human-presence detection.
- The first live state and subsequent state changes enter the timeline. Repeated true/false reports update diagnostics and last-report timestamps without creating duplicate events.
- Today's detections counts true state changes since midnight in the database timezone (`Europe/Kyiv` on this installation). Clear events are displayed but not counted as detections.
- State comparison occurs inside a database transaction with a row lock. The baseline survives collector restarts.
- Partial battery/tamper/signal reports preserve the previous occupancy and its observation timestamp. Null, malformed and non-finite values never become invented telemetry.
- Retained MQTT snapshots are ignored: they have no trustworthy new-event timestamp. No historical detections are reconstructed from cached MQTT data or logs.
- History starts when the integration is enabled. Timestamps are the Pi's receipt time, with a timezone; the UI displays them in the browser's locale.
- Quiet battery devices are not marked offline using the ESP32's 15-second timeout. The UI explicitly shows the **last reported state** and last-report time, not a claimed heartbeat. It does not assert that a sensor is reachable merely because it is paired.
- Zigbee2MQTT's default live reports are not a durable event queue. Reports during collector/database/broker outages can be lost; the integration does not fabricate or backfill them. MQTT subscription QoS 1 cannot upgrade a publisher's QoS 0.
- The Zigbee card shows the configured, integrated sensor. It is not a full-network inventory. Coordinator device presence comes from the existing System Status probe and is not a Zigbee2MQTT service-health check.

## API and diagnostics

```sh
curl 'http://localhost:8000/api/motion?hours=24&limit=20'
mosquitto_sub -h localhost -t 'zigbee2mqtt/0xa4c138657fddb903' -v
journalctl -u smarthome-collector -f
```

The API returns `configured`, `device`, `sensor`, `events`, `today_count`, `day_timezone`, `hours` and `generated_at`. A configured sensor with no live readings has `sensor: null`, an empty timeline and zero recorded detections. Query limits are 1–168 hours and 1–100 events. PostgreSQL failures return a safe HTTP 503 response. Responses are not cached by the browser; overlap prevention, request timeouts and changed-value DOM updates keep polling lightweight.

The Motion card shows state, last motion, today's count, battery, LQI and last report, plus the latest 20 recorded changes within 24 hours. Existing dark/light themes and responsive module layout are preserved. Wi-Fi CSI and camera remain uninstalled. No automations, notifications, device renaming or pairing changes are made by this integration.

## Tests

```sh
cd ~/smarthome
venv/bin/python -m unittest discover -s tests -v

# Optional PostgreSQL tests: use existing DB environment variables.
# Creates a uniquely named test schema and removes only that schema afterward.
RUN_MOTION_DB_TESTS=1 venv/bin/python -m unittest discover -s tests -p test_motion.py -v
```

`tests/check_motion_browser.py` is an optional Playwright check against intercepted fixtures only. Run it in an isolated development environment with Playwright installed and Chrome available (`CHROME_PATH` overrides the executable path). `MOTION_SCREENSHOT_DIR` optionally saves screenshots. It tests both themes, four screen widths, motion/clear states, zero diagnostics, API failure/recovery, waiting/disabled states, safe text rendering, unchanged timeline nodes and existing chart controls. Test data is never published to the real MQTT broker.

Protocol reference: https://www.zigbee2mqtt.io/devices/TS0202.html
