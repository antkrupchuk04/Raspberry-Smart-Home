# System Status

`GET /api/system/status` provides real host and dependency health through `system_status.py`. The existing card and header use this endpoint; their visual structure and theme are retained. Other endpoints, MQTT collection and existing polling intervals are unchanged.

## Install and operate

On the Raspberry Pi:

```bash
cd ~/smarthome
source venv/bin/activate
python -m pip install psutil
# For a complete install instead: python -m pip install -r requirements.txt
sudo systemctl restart smarthome-api
sudo systemctl status smarthome-api --no-pager
curl http://localhost:8000/api/system/status
python -m unittest discover -s tests -v
```

Only the API needs restarting. No collector, MQTT broker or database restart is required. `psutil` is the only new dependency and is included in `requirements.txt`.

## Probes and caching

| Check | Method | Cache |
| --- | --- | --- |
| CPU | Nonblocking differences between system-wide `psutil.cpu_times()` samples | 5 seconds |
| RAM | `psutil.virtual_memory()`; used = total minus available | 5 seconds |
| Root disk | `psutil.disk_usage('/')` | 5 seconds |
| CPU temperature | `/sys/class/thermal/thermal_zone0/temp`, millidegrees converted to °C | 5 seconds |
| Host | `socket.gethostname()` and `psutil.boot_time()` | 5 seconds; uptime recalculated per response |
| MQTT | TCP connection to existing `MQTT_BROKER` / `MQTT_PORT` | 5 seconds |
| PostgreSQL | Existing `DB_CONFIG`, `SELECT 1`, then latest `recorded_at` | 5 seconds |
| ESP32 | Age of the actual latest database timestamp; online for less than 15 seconds | Timestamp shares DB cache; age recomputed per response |
| Internet | TCP connection to `1.1.1.1:443` | 45 seconds |
| API | Endpoint is running; monotonic application uptime | No external check |
| Zigbee | Existence of explicitly configured serial device path | 30 seconds |

TCP timeouts are 0.75 seconds. PostgreSQL connections have a two-second connect timeout and a one-second statement timeout. Connections are explicitly closed after the checks. No probe publishes telemetry, pings the ESP32, calls the API itself, runs shell commands, or creates a background loop.

Caches are per API process with per-probe locks, so concurrent requests share checks. Expected failures are cached too. Each probe has an independent fallback; an unavailable temperature sensor returns `null`, a failed broker connection reports `offline`, and a database failure reports `error` plus unknown ESP32 state. Expected health failures return HTTP 200. Unexpected individual probe failures are logged once per failure episode, with a safe fallback in the response. Exceptions and credentials never appear in JSON.

The first CPU sample after an API restart is `null` until a second sample at least five seconds later establishes a real interval. The shared counter baseline works across FastAPI worker threads without blocking. This follows the nonblocking sampling considerations in the [psutil documentation](https://psutil.readthedocs.io/stable/). GB values use 1024³ bytes, and percentages come from psutil.

## Thresholds and overall state

All limits are in `SYSTEM_THRESHOLDS` and are returned to the frontend so it uses the same thresholds for colors:

```python
SYSTEM_THRESHOLDS = {
    "cpu_warning": 80,
    "memory_warning": 85,
    "disk_warning": 85,
    "cpu_temp_warning": 70,
    "cpu_temp_critical": 80,
    "esp32_timeout_seconds": 15,
}
```

- **Healthy:** core dependencies and ESP32 are available and no active warnings exist.
- **Warning:** high CPU/RAM/disk usage, high temperature, missing metrics, unavailable Internet, stale/unknown ESP32, or a configured Zigbee device missing.
- **Degraded:** MQTT/database/API dependency failure or critical CPU temperature.
- An unconfigured optional Zigbee coordinator does **not** affect overall health. Its own row is amber to identify the optional integration.

The response includes fixed, safe `alerts` with component, severity and message. Thresholds and probe TTLs are centralized for later extension.

## Frontend

The existing card displays CPU/RAM/disk percentages, CPU temperature, uptime, hostname and all six service states. Hovering RAM/disk reveals used/total GB. Uptime also fills the existing header slot, and the header status reflects aggregate health. The card footer shows the last update and active warning messages.

One new poller calls `/api/system/status` every ten seconds. It skips overlapping requests, aborts after eight seconds, and changes DOM text only when values change. The existing sensor loader no longer writes into the System Status card. The local clock can expire ESP32 status between polls without adding network traffic. When monitoring cannot be fetched, metrics clear to unavailable and dependency states become unknown rather than displaying old green indicators.

Existing green/amber/red/gray theme colors indicate healthy/warning/failure/unknown states. The card, grid, typography and spacing are preserved.

## Example real reading

This excerpt was read on the Pi during deployment validation, not generated as demo telemetry:

```json
{
  "generated_at": "2026-09-14T17:04:26.294565+00:00",
  "overall_status": "healthy",
  "host": {"hostname": "raspberry", "platform": "Raspberry Pi", "uptime_seconds": 4272, "uptime": "1h 11m"},
  "cpu": {"usage_percent": 0.6, "temperature_c": 34.2},
  "memory": {"used_gb": 0.7, "total_gb": 7.9, "usage_percent": 8.6},
  "disk": {"used_gb": 6.4, "total_gb": 56.8, "usage_percent": 11.7},
  "services": {
    "mqtt": {"status": "online", "host": "localhost", "port": 1883},
    "database": {"status": "connected", "query_ok": true},
    "internet": {"status": "online"},
    "esp32": {"status": "online", "last_seen": "2026-09-14T17:04:26.049152+00:00", "age_seconds": 0.2},
    "api": {"status": "running", "uptime_seconds": 5},
    "zigbee": {"configured": false, "device_present": false, "status": "not_configured"}
  },
  "alerts": []
}
```

The full response also contains the centralized `thresholds` object.

## Optional Zigbee

To enable detection, set an actual coordinator path in the existing `.env`, then restart the API:

```dotenv
ZIGBEE_SERIAL_DEVICE=/dev/serial/by-id/your-coordinator
```

`/dev/ttyUSB0` or `/dev/ttyACM0` can also be explicitly configured. Unrelated USB serial devices are never assumed to be Zigbee. The JSON returns `configured` and `device_present` separately. `device_present` confirms only the configured path exists; it does not claim Zigbee protocol health. Zigbee2MQTT communication, device inventory, signal quality and network mapping remain future work.

## Tests

`tests/test_system_status.py` mocks system, database and network operations to cover metric serialization, CPU sampling, temperature parsing, uptime, MQTT/Internet success and failure, database connections/queries/closure, ESP32 freshness boundaries, Zigbee configuration, isolated failures, thresholds, aggregate states, TTL behavior, concurrency and safe logging.

`tests/test_system_status_api.py` verifies existing routes remain registered and expected monitoring failures still produce HTTP 200 with safe JSON. No real Internet, MQTT broker, database credentials or systemd access is required by these tests.
