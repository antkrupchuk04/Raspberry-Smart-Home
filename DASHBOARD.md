# Dashboard architecture

The dashboard is a refactor of the existing vanilla HTML/CSS/JavaScript client. Existing FastAPI endpoints, PostgreSQL storage, MQTT collection and refresh intervals are preserved. A dedicated System Status endpoint now supplies real host and dependency health. The existing dark/light tokens, fonts, borders, shadows, card padding, grid gaps and status animation remain the visual foundation.

## Layout

```text
Header: Smart Home · Raspberry Pi 5 · time · uptime · connection · status · theme
Navigation: Environment / History / Motion / Automation / Devices

Current Environment / Room 1
┌───────────────────┬───────────────────┬───────────────────┐
│ Temperature / 4   │ Humidity / 4      │ Pressure / 4      │
└───────────────────┴───────────────────┴───────────────────┘
┌───────────────────────────────────────┬───────────────────┐
│ History / 8                           │ Weather / 4       │
│                                       ├───────────────────┤
│ Metric · range · CSV · fullscreen      │ AI Analysis       │
│                                       ├───────────────────┤
│                                       │ System Status     │
└───────────────────────────────────────┴───────────────────┘
│ Motion / 12 · sensor readiness · last event · timeline     │
│ Automation / 12 · lights · modes · heating                 │
│ Camera / 12 · latest snapshot · time · motion detection    │
│ Zigbee / 12 · coordinator · device types · signal quality  │
```

- Above 1024px, CSS Grid provides twelve equal tracks; sensors span four each, history spans eight, and its stacked sidebar spans four. History stretches to the sidebar height. Sensor cards have compact, content-sized rows independent of history.
- From 601px through 1024px, sensors stay three across, while history, weather, AI, system and all later modules use full width.
- At 600px and below, all cards stack in DOM reading order: temperature, humidity, pressure, history, weather, AI, system, motion, automation, camera, Zigbee.
- Structural sections use real grid containers. There is no CSS `order`, dense placement, or `display: contents` dependency that could separate keyboard order from visual order.

## Files and reusable components

| File | Responsibility |
| --- | --- |
| `static/index.html` | Semantic regions, anchor navigation, shared card shell and body templates |
| `static/style.css` | Original theme tokens, reusable card/field/toolbar styles and responsive grids |
| `static/app.js` | Card creation, sensor/module configuration, existing API loaders and canvas chart |

`card-template` defines the header, title, subtitle, badge, body and footer once. `createCard()` clones it, assigns a unique accessible heading, and inserts the requested body template. `MODULES` supplies module metadata, and each `data-module` host chooses where that card lives.

The three first-class sensors are configured in `SENSORS`. They use one `sensor-body` and one `sensor-footer` template. All three show a value, trend, minimum, maximum, average and reading timestamp. Pressure now has the same statistics as the other sensors. Statistics use the selected history window; trend is the first-to-last change divided by the actual elapsed hours. Null/missing readings stay unavailable and are excluded from chart/statistics calculations; valid zero readings are preserved.

## Data and performance

| Data | Existing endpoint | Interval |
| --- | --- | --- |
| Current sensors and ESP32 freshness | `/api/sensors/latest` | 5 seconds |
| Selected history window | `/api/sensors/history?hours=1`, `6`, or `24` | 10 seconds |
| Comfort, insights, anomaly and forecast | `/api/analytics/room` | 30 seconds |
| Outdoor weather | `/api/weather/current` | 10 minutes |
| Host and dependency health | `/api/system/status` | 10 seconds |
| PIR state, diagnostic values and motion timeline | `/api/motion?hours=24&limit=20` | 5 seconds |

System Status has one independent ten-second poller with request timeout and overlap protection. Switching chart metric reuses the fetched history. Range changes cancel the previous request so a slow response cannot replace a newer selection. Duplicate current/history requests cannot overlap. DOM text changes only when the value changes, AI messages are compared before replacing nodes, and unchanged history avoids redundant chart rendering. A `ResizeObserver` resizes the canvas when its container changes, including fullscreen transitions. The existing one-second clock updates freshness locally without another request.

CSV export downloads the selected window already in memory, containing UTC timestamps and all three metrics. Fullscreen uses the browser API, with an expanded-card fallback that Escape closes. Seven- and thirty-day controls are visible but disabled because the current history endpoint supports at most 24 hours.

## System status and future modules

The existing card now uses `/api/system/status` for real CPU, RAM, disk, temperature, uptime, hostname, MQTT TCP reachability, PostgreSQL `SELECT 1`, Internet connectivity, ESP32 freshness and optional Zigbee device presence. Overall status includes host thresholds and dependency failures. CPU counters warm up on the first request after restart; missing metrics show unavailable without breaking the remaining checks. See [System Status](SYSTEM_STATUS.md) for caching, thresholds, configuration, response structure and operational commands.

AI recommendations use the actual comfort messages returned by the existing analysis endpoint. Its already-implemented anomaly result remains visible. No cloud AI or new recommendation model is introduced.

Motion now records the Living room Tuya TS0202 through Zigbee2MQTT and the existing collector. Its card displays the last reported PIR state, last motion, today's detections, battery, LQI, last report and a bounded timeline. The Zigbee card shows this integrated sensor and the coordinator's serial-device presence. It does not claim to be a full network inventory. See [Motion integration](MOTION.md) for state-change counting, retained-message handling and collection limits.

Automation and camera remain compact future-module placeholders. Snapshot and network-map controls remain disabled. The standalone RSSI motion script is separate from the planned Wi-Fi CSI integration.

## Adding rooms and modules

To add a module, add its body template, one `MODULES` entry, and a `data-module` host in the appropriate region. The shared card shell, field grids and responsive rules apply automatically. Boiler, garage, garden, solar, battery and UPS cards can use this same path without changing the page grid.

Sensor metadata belongs in `SENSORS`; layout and statistics operate on that configuration. Additional room sections can reuse the sensor grid and templates with unique room-scoped IDs and a corresponding data adapter. The present API returns the latest reading across the database, so multi-room selection will also require room/device filtering in a future backend change. This refactor does not pretend that capability already exists.

## Validation

Headless Chrome checked real API response fixtures at 1440, 1280, 1024, 768, 601, 600, 390 and 320px, in both themes: equal sensor sizes, correct reading order, sidebar alignment, and no horizontal overflow. Functional checks covered metric/range selection, out-of-order range responses, null and zero readings, stale/empty/error/recovery states, CSV export, fullscreen, persistent theme and JavaScript exceptions. Polling registration was verified against the unchanged intervals above.
