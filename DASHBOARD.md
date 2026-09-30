# Dashboard architecture

The dashboard is a refactor of the existing vanilla HTML/CSS/JavaScript client. Existing FastAPI endpoints, PostgreSQL storage, MQTT collection and refresh intervals are preserved. A dedicated System Status endpoint now supplies real host and dependency health. The existing dark/light tokens, fonts, borders, shadows, card padding, grid gaps and status animation remain the visual foundation.

## Layout

```text
Header: Smart Home · Raspberry Pi 5 · time · uptime · connection · status · theme
Navigation: Environment / History / Smart Home Map (/topology) / Motion / Automation / Devices

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
| `static/topology.html` | Separate map page, reusable node button, details panel and local SVG symbol library |
| `static/topology.js` | Device type/icon registry, keyed node updates, selection, details and SVG branches |
| `static/topology-page.js` | Map-only polling, clock/header and shared theme preference |

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
| Configured-device topology | `/api/topology` | 10 seconds on the visible map page only |

System Status has one independent ten-second poller with request timeout and overlap protection. Switching chart metric reuses the fetched history. Range changes cancel the previous request so a slow response cannot replace a newer selection. Duplicate current/history requests cannot overlap. DOM text changes only when the value changes, AI messages are compared before replacing nodes, and unchanged history avoids redundant chart rendering. A `ResizeObserver` resizes the canvas when its container changes, including fullscreen transitions. The existing one-second clock updates freshness locally without another request.

CSV export downloads the selected window already in memory, containing UTC timestamps and all three metrics. Fullscreen uses the browser API, with an expanded-card fallback that Escape closes. Seven- and thirty-day controls are visible but disabled because the current history endpoint supports at most 24 hours.

## System status and future modules

The existing card now uses `/api/system/status` for real CPU, RAM, disk, temperature, uptime, hostname, MQTT TCP reachability, PostgreSQL `SELECT 1`, Internet connectivity, ESP32 freshness and optional Zigbee device presence. Overall status includes host thresholds and dependency failures. CPU counters warm up on the first request after restart; missing metrics show unavailable without breaking the remaining checks. See [System Status](SYSTEM_STATUS.md) for caching, thresholds, configuration, response structure and operational commands.

AI recommendations use the actual comfort messages returned by the existing analysis endpoint. Its already-implemented anomaly result remains visible. No cloud AI or new recommendation model is introduced.

Motion now records the Living room Tuya TS0202 through Zigbee2MQTT and the existing collector. Its card displays the last reported PIR state, last motion, today's detections, battery, LQI, last report and a bounded timeline. The Zigbee card shows this integrated sensor and the coordinator's serial-device presence. It does not claim to be a full network inventory. See [Motion integration](MOTION.md) for state-change counting, retained-message handling and collection limits.

Automation and camera remain compact future-module placeholders. Snapshot and network-map controls remain disabled. The standalone RSSI motion script is separate from the planned Wi-Fi CSI integration.

## Smart Home Map

The **Smart Home Map** navigation entry opens `GET /topology`, served by FastAPI from `static/topology.html`. The main dashboard no longer embeds or polls the map. Old `/#topology` links redirect to this page. Navigation returns to the dashboard, Motion or Devices, and both pages use the same CSS/theme tokens and saved `theme` preference.

The canvas shows a prominent Raspberry Pi hub above grouped Wi-Fi/MQTT and Zigbee devices. Each device is a real button with a local SVG icon, name, room, concise health and optional active condition. Readings and service diagnostics appear only in the selected device's details panel. There are no new runtime dependencies or copied brand assets.

### Interaction and rendering

`topology-node` defines one reusable button shell. `MAP_DEVICE_TYPES` maps normalized types to SVG symbols and optional state formatters: `hub`, `esp32`, `zigbee_motion`, `zigbee_coordinator`, `zigbee_water_leak`, `zigbee_smart_plug`, `zigbee_contact` and `generic_sensor`. Unknown types use the generic icon. Prepared future icons do not create devices or controls.

Click/tap, Enter or Space selects a device and focuses its detail heading. Only one panel opens at a time. It shows device identifiers, type, room, connection, report age, readings, diagnostics and explanatory notes. Close/Escape restores focus to the selected node. Selection and node identity survive live updates; removal of a selected device closes the panel and restores focus to an available node. Values use text nodes, and unchanged text/paths are not rewritten.

At 900px and wider, the panel sits to the right of the map and scrolls independently when needed. On narrower screens it occupies full width below the map and scrolls into view when selected. Technology groups use CSS Grid and stack when space is limited; devices within each group use Flexbox. Empty groups are omitted. `ResizeObserver` measures actual node/group bounds for neutral shared SVG trunks and separately colored device branches. No device has hardcoded coordinates, and mobile branches do not imply a chain of device-to-device connections.

A new recent PIR detection briefly pulses its icon once. Unchanged reports, initial snapshots and API recovery do not continuously animate. Reduced-motion preference disables the pulse. Health remains separate from the last reported motion condition.

### Model and health semantics

`topology_service.py` constructs the read-only model through `TopologyService`. `api.py` registers `esp32-room1` and the configured PIR (`motion-room1` as a logical map ID, with its physical IEEE address preserved). An unconfigured PIR is omitted. The endpoint publishes nothing and changes no MQTT topics, device IDs or database schema.

`GET /api/topology` returns:

- `generated_at`, `refresh_seconds`, `status` (`ok` or `partial`) and per-source `sources` availability.
- `system.status`: the existing system monitor's aggregate healthy/warning/degraded result, or unknown when unavailable.
- `nodes`: stable `id`, normalized `type`, name/room, physical `device_id`, health status/label, data status, metrics, details and note. Reported devices include `last_seen` and `age_seconds`; PIR nodes include `activity` and its observation timestamp. Field descriptors contain key, label, value, kind, unit, precision and optional semantic state; CSS colors belong to the client.
- `links`: stable ID, source/target IDs, protocol, transport, label, status and an explanation of the evidence behind it.

The map summary reuses system health: **System healthy** or **Attention required**. **Partial data**, **Map unavailable** and **Awaiting update** explicitly describe map data/freshness instead of redefining host health. Hub details show CPU/RAM/disk/temperature, uptime, FastAPI, MQTT, PostgreSQL, Internet and USB coordinator presence. Zigbee2MQTT health is **Not monitored**: serial-device presence is not proof of protocol health.

Climate queries filter by configured device ID. Each device reuses `esp32_status()` and `SYSTEM_THRESHOLDS`, including the 15-second timeout and invalid/future timestamp handling. The backend supplies the remaining `online_for_seconds` lease, which the local clock expires between requests. There is no second frontend freshness threshold. Stored readings remain visible when a device goes offline. ESP32 RSSI is not fabricated.

PIR occupancy is the **last reported motion state**, not continuous presence or a heartbeat. Battery/LQI/voltage and timestamps come from `motion_sensor_state`. Quiet sensors are not declared offline: availability stays unknown. Motion detected / No motion is displayed separately from that health status.

A green Wi-Fi/MQTT branch means fresh stored telemetry and a reachable broker. Stale telemetry or a known MQTT failure changes its state. Zigbee branches remain neutral/unverified without reliable availability; a known broker failure or missing configured coordinator makes the branch unavailable. Hub details and the device note explain the simplified Zigbee → coordinator → Zigbee2MQTT → MQTT path. This is a configured-device map, not a discovered Zigbee mesh.

### Queries, refresh and failures

Only the map page polls `/api/topology`, every ten seconds while visible, with an eight-second timeout and overlap protection. Visibility changes resume fetching. Its header derives health/uptime from the same snapshot, without separate system-status requests. A one-second local clock updates report ages and expiry without network traffic. Main dashboard intervals remain unchanged.

Topology reuses `SystemStatusMonitor` and its existing cached probes. Two read-only telemetry queries are cached for five seconds: latest climate readings grouped by configured device ID and the current states of configured PIRs. They reuse existing credentials, parameterized IDs, two-second connect timeout and one-second statement timeout. Connections close after use. Failed reads are cached and logged once per failure episode without exception details. No MQTT subscriber or backend background loop is added.

A failed source returns partial HTTP 200 data while other nodes continue updating. HTTP caching is disabled. Initial frontend failure shows an unavailable message; later failure retains the previous snapshot with stale labels, outdated detail values, and no green links or live motion emphasis. Overdue snapshots are also marked stale. Loading and empty-device states are explicit. Missing numeric fields render as unavailable; valid zeros are retained.

### Adding devices and current limits

For another climate or PIR device already collected into PostgreSQL, extend the `TopologyService` deployment registry in `api.py` with a unique map ID and climate descriptor or `MotionConfig`. Registered devices are queried together and rendered through the same arrays and branch geometry. The current collector still handles one ESP32 and one PIR; collection/configuration of additional devices remains separate work.

For an **Aqara Water Leak Sensor**, after pairing it with Zigbee2MQTT:

1. Confirm its real topic, identifier and exposed payload fields, then add collector/storage support for its actual `water_leak`, battery and report data.
2. Add a small backend adapter/registry entry returning a `zigbee_water_leak` node and Zigbee link, with real metric/detail descriptors and an optional human-readable `activity_label` such as Water detected. Leave availability unknown until trustworthy reporting exists.
3. Add mapping tests and a browser fixture. The water icon, generic details and grouped layout already work; individual HTML or SVG coordinates are unnecessary.

No water-leak detection/control is implemented by preparing its icon. Automatic inventory/discovery, Zigbee bridge/device availability and mesh routing are not implemented. Coordinator presence is only a device-path check. Existing climate APIs remain centered on one room. RSSI motion monitoring is a separate experiment; Wi-Fi CSI remains planned.

### Map validation

The complete backend suite passes **99 tests**, including existing isolated PostgreSQL checks. Topology coverage includes JSON structure and aggregate status, FastAPI diagnostics, hub/ESP32 mapping, freshness boundaries, PIR state vs availability, evidence-based links, isolated failures, caching, query bounds/cleanup, additional devices, route preservation and the separate HTML page.

`tests/check_topology_browser.py` uses intercepted local fixtures with optional Playwright/Chrome. It passes at 1440, 1024, 1000, 768, 390 and 320px in both themes, checking SVG/card geometry, panel placement, keyboard/focus, live detail updates, persistent nodes, motion/clear/zero values, reduced motion, expiry, partial/error/recovery/empty states, extra nodes, safe text, navigation, saved theme and map-only polling. `tests/check_motion_browser.py` also passes at four widths in both themes, including existing chart controls. Fixtures are never published to MQTT or sent to Telegram; browser tooling is not a runtime dependency.

## Adding rooms and modules

To add a module, add its body template, one `MODULES` entry, and a `data-module` host in the appropriate region. The shared card shell, field grids and responsive rules apply automatically. Boiler, garage, garden, solar, battery and UPS cards can use this same path without changing the page grid.

Sensor metadata belongs in `SENSORS`; layout and statistics operate on that configuration. Additional room sections can reuse the sensor grid and templates with unique room-scoped IDs and a corresponding data adapter. The present API returns the latest reading across the database, so multi-room selection will also require room/device filtering in a future backend change. This refactor does not pretend that capability already exists.

## Validation

Headless Chrome checked real API response fixtures at 1440, 1280, 1024, 768, 601, 600, 390 and 320px, in both themes: equal sensor sizes, correct reading order, sidebar alignment, and no horizontal overflow. Functional checks covered metric/range selection, out-of-order range responses, null and zero readings, stale/empty/error/recovery states, CSV export, fullscreen, persistent theme and JavaScript exceptions. Polling registration was verified against the unchanged intervals above.
