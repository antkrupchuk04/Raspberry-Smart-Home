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
| Live ESP32 CSI state and diagnostics | `/api/motion/csi` | 5 seconds, independently of the PIR refresh |
| Configured-device topology | `/api/topology` | 10 seconds on the visible map page only |

System Status has one independent ten-second poller with request timeout and overlap protection. Switching chart metric reuses the fetched history. Range changes cancel the previous request so a slow response cannot replace a newer selection. Duplicate current/history requests cannot overlap. DOM text changes only when the value changes, AI messages are compared before replacing nodes, and unchanged history avoids redundant chart rendering. A `ResizeObserver` resizes the canvas when its container changes, including fullscreen transitions. The existing one-second clock updates freshness locally without another request.

CSV export downloads the selected window already in memory, containing UTC timestamps and all three metrics. Fullscreen uses the browser API, with an expanded-card fallback that Escape closes. Seven- and thirty-day controls are visible but disabled because the current history endpoint supports at most 24 hours.

## System status and future modules

The existing card now uses `/api/system/status` for real CPU, RAM, disk, temperature, uptime, hostname, MQTT TCP reachability, PostgreSQL `SELECT 1`, Internet connectivity, ESP32 freshness and optional Zigbee device presence. Overall status includes host thresholds and dependency failures. CPU counters warm up on the first request after restart; missing metrics show unavailable without breaking the remaining checks. See [System Status](SYSTEM_STATUS.md) for caching, thresholds, configuration, response structure and operational commands.

AI recommendations use the actual comfort messages returned by the existing analysis endpoint. Its already-implemented anomaly result remains visible. No cloud AI or new recommendation model is introduced.

Motion now records the Living room Tuya TS0202 through Zigbee2MQTT and the existing collector. Its card displays the last reported PIR state, last motion, today's detections, battery, LQI, last report and a bounded timeline. The Zigbee card shows this integrated sensor and the coordinator's serial-device presence. It does not claim to be a full network inventory. See [Motion integration](MOTION.md) for state-change counting, retained-message handling and collection limits.

Automation and camera remain compact future-module placeholders. Snapshot and network-map controls remain disabled. The standalone RSSI motion script is separate from the live ESP32 CSI display.

## Live Wi-Fi CSI display

`csi_service.py` provides a single lightweight Paho subscriber during FastAPI's lifespan, using the existing `MQTT_BROKER`/`MQTT_PORT`. Its unique client ID avoids collisions with the ESP32 and collector. Only the exact `CSI_MQTT_TOPIC` is subscribed (default `home/motion/room1/csi`) and reports must match `CSI_DEVICE_ID` (default `esp32-room1`). It never publishes messages. Confirmed transitions are submitted to the independent `csi_history.py` queue worker for PostgreSQL storage. No runtime dependency was added.

`GET /api/motion/csi` returns HTTP 200/no-store with feed status (`live`, `waiting`, `stale`, `mqtt_offline`), firmware state, filtered `motion`, original `raw_motion`, score, firmware thresholds, effective filter configuration/thresholds, calibration count, samples, RSSI, drop counters, receipt time, report age, remaining freshness lease and last detection, recent persisted `events` and `history_available`. Optional `hours` (1–168) and `limit` (1–100) default to 24/20. Invalid, oversized, non-finite, wrong-device and retained reports are ignored. Expected offline states do not spam logs; invalid-report warnings are bounded to one per failure episode. Reconnecting requires a new live report.

Freshness uses a centralized five-second threshold and a monotonic clock. Outside a fresh ready state, motion is null; stale/disconnected reports have no current numerical readings. Settling/calibrating/no-data are explicitly unknown rather than clear. The remaining lease is returned so the existing one-second browser clock can expire the display locally without inventing another timeout. The browser fetches CSI independently every five seconds, with timeout and overlap protection. Filtering processes each one-second MQTT report on the server, so it does not depend on browser polling. PIR, climate, chart and system-status intervals remain unchanged. Failure of the PIR API does not suppress CSI, and CSI failure does not suppress PIR.

The existing Motion card's CSI field shows filtered live status, and an ESP32 raw-state field exposes the source flag independently. A compact subsection uses existing field-grid and theme styles for score, detection/clear thresholds, calibration windows, samples/window, actual Wi-Fi RSSI, dropped frames, last CSI detection and report time. Missing values render as unavailable, including thresholds before calibration completes; numeric zero remains valid. No continuous-presence claim or confidence percentage is displayed.

`CsiMotionFilter` uses centralized `CSI_FILTER` settings: score multiplier 1.25, three confirmation reports, three clearing reports, and a maximum 2.5-second gap. A report counts toward activation only when the firmware motion flag is true and its score reaches 1.25× the firmware on-threshold. Consecutive low scores below 1.25× the firmware off-threshold, or raw-clear reports, clear the filtered state after three reports. This avoids treating the firmware's hysteresis-latched flag alone as sustained activity. Isolated high scores reset confirmation instead of accumulating. No-data/calibration, reconnects, source restarts, long source/receipt gaps and threshold changes reset the filter; duplicate ready reports with the same source uptime do not count or refresh freshness. Last detection timestamps update only on confirmed filtered transitions. The API retains original firmware thresholds and provides effective thresholds in `filter`; the UI displays the latter. These conservative defaults can suppress brief genuine motion and still need room-specific validation.

The Motion card has a separate **Wi-Fi CSI timeline / Last 24 hours**, alongside the explicitly labeled Zigbee PIR timeline. It uses the shared timestamp/state/room row renderer and existing theme/grid styles, shows the latest 20 confirmed changes newest first, and preserves unchanged DOM rows. Five-second polling updates the CSI state and list together; events are captured independently of the browser. Empty, unavailable, stale and recovery states remain distinct. CSI does not contribute to the PIR event count or Telegram notifications, and the separate Smart Home Map is unchanged.

`csi_motion_events` stores UUID, device ID, room, filtered motion boolean, score, effective thresholds and timezone-aware Pi observation time. The compound device/time index bounds history reads. Filter activation emits one detected event; three quiet reports emit one clear. Repeated reports and page refreshes add nothing. Calibration, stale data, threshold/channel gaps and disconnect/restart resets never invent clears. History begins at enablement; older CSI events cannot be reconstructed.

The additive `initialize_schema()` migration creates only the CSI table/index; it is repeatable and preserves other tables. The existing database settings are reused. Run it explicitly before restarting the updated API:

```bash
cd ~/smarthome
set -a
. ./.env
set +a
venv/bin/python -c 'from api import DB_CONFIG; from csi_history import initialize_schema; initialize_schema(DB_CONFIG)'
sudo systemctl restart smarthome-api
```

A bounded 128-event queue keeps PostgreSQL work out of the MQTT callback. Failed writes retry the same UUID, with `ON CONFLICT DO NOTHING` preventing duplicate commits. Committed history survives process restarts. The queue itself is in memory: queued events can be lost on shutdown during an outage or on overflow, and reports during API downtime are not reconstructed. Logging is bounded by failure episode. Reads and writes use one-second connect and 750ms statement timeouts. History failures are reported separately while live CSI continues. No PostgreSQL, collector or broker service restart is required. `CSI_ROOM` optionally labels recorded events, defaulting to Room 1; the private `.env` is preserved.

Last CSI detection uses the latest in-process confirmation when available and falls back to the database after restart. There is no synthetic initial clear on startup and no backfill from raw MQTT samples.

`tests/test_csi.py` covers parsing, freshness boundaries, weak/isolated spikes, sustained confirmation, stable clearing, gap/reboot/threshold resets, unknown states, MQTT disconnect/reconnect, retained/duplicate/invalid reports, confirmed transition timestamps and lifecycle/API isolation without a real broker. Additional `tests/test_csi_history.py` cases cover retries/UUID idempotency, bounded queues, timeouts, query limits/device isolation, empty history and repeatable migration/restart persistence in isolated PostgreSQL schemas. Dashboard browser checks also cover separate timestamped CSI/PIR lists, unchanged row identity, empty/history-error recovery, safe text rendering, CSI states and local expiry at four widths in both themes.

## Smart Home Map

The **Smart Home Map** navigation entry opens `GET /topology`, served by FastAPI from `static/topology.html`. The main dashboard no longer embeds or polls the map. Old `/#topology` links redirect to this page. Navigation returns to the dashboard, Motion or Devices, and both pages use the same CSS/theme tokens and saved `theme` preference.

The canvas centers a prominent Raspberry Pi computer/hub icon above Wi-Fi/MQTT and Zigbee groups. Devices are floating, transparent buttons rather than rectangular cards, with a local circular SVG icon, name, room, concise health and optional active condition. The header gives the device count and aggregate status once; helper text appears only for loading or unavailable/partial data. Readings and service diagnostics appear only in the selected device's details panel. There are no new runtime dependencies or copied brand assets.

### Interaction and rendering

`topology-node` defines one reusable button shell. `MAP_DEVICE_TYPES` maps normalized types to SVG symbols and optional state formatters: `hub`, `esp32`, `zigbee_motion`, `zigbee_coordinator`, `zigbee_water_leak`, `zigbee_smart_plug`, `zigbee_contact` and `generic_sensor`. Unknown types use the generic icon. Prepared future icons do not create devices or controls.

Click/tap, Enter or Space selects a device and focuses its detail heading. Only one panel opens at a time. It shows device identifiers, type, room, connection, report age, readings, diagnostics and explanatory notes. Activating the selected node again closes its details; activating a different node switches the panel. Close/Escape also closes the panel and restores focus to the selected node. Selection and node identity survive live updates; removal of a selected device closes the panel and restores focus to an available node. Values use text nodes, and unchanged text/paths are not rewritten. Selection uses an accent ring around the icon and an emphasized label. Keyboard focus uses a separate dashed outer icon outline, without a full-node rectangle. Hover feedback stays within the icon and causes no layout shift. Long detail values wrap within a bounded key/value grid.

At 900px and wider, the panel sits to the right and scrolls independently when needed. Opening/closing it briefly animates the available map width; the hub re-centers in that space. Closing restores the full-width canvas without reserving an empty drawer column. On narrower screens it occupies full width below the map and scrolls into view when selected. Technology groups use CSS Grid and stack when space is limited, always stacking at 600px and below; devices within each group use Flexbox. Empty groups are omitted. `ResizeObserver` measures actual node, icon and protocol-label bounds to draw rounded neutral SVG paths. Each branch starts beneath the hub, passes through its protocol label and ends just above the target icon. Wrapped groups use a shared outside gutter; groups with multiple devices use a separate branch spine so lines avoid preceding nodes. No device has hardcoded coordinates, and mobile branches do not imply a chain of device-to-device connections.

A new recent PIR detection briefly pulses its icon once. Unchanged reports, initial snapshots and API recovery do not continuously animate. Reduced-motion preference disables the pulse and layout/icon transitions. Health remains separate from the last reported motion condition.

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

The backend link fields remain unchanged: Wi-Fi/MQTT evidence combines stored telemetry freshness with broker reachability, while Zigbee availability is unverified unless an existing probe identifies a failure. These fields do not establish direct radio-link health. V3 therefore renders all communication paths as neutral, solid lines, without inferring connection health from device health or suggesting unknown Zigbee links are broken. Status and freshness remain visible on nodes and in their details. Hub details and the device note explain the simplified Zigbee → coordinator → Zigbee2MQTT → MQTT path. This is a configured-device map, not a discovered Zigbee mesh.

### Queries, refresh and failures

Only the map page polls `/api/topology`, every ten seconds while visible, with an eight-second timeout and overlap protection. Visibility changes resume fetching. Its header derives health/uptime from the same snapshot, without separate system-status requests. A one-second local clock updates report ages and expiry without network traffic. Main dashboard intervals remain unchanged.

Topology reuses `SystemStatusMonitor` and its existing cached probes. Two read-only telemetry queries are cached for five seconds: latest climate readings grouped by configured device ID and the current states of configured PIRs. They reuse existing credentials, parameterized IDs, two-second connect timeout and one-second statement timeout. Connections close after use. Failed reads are cached and logged once per failure episode without exception details. No MQTT subscriber or backend background loop is added.

A failed source returns partial HTTP 200 data while other nodes continue updating. HTTP caching is disabled. Initial frontend failure shows an unavailable message; later failure retains the previous snapshot with stale labels, outdated detail values and no live motion emphasis. Neutral topology paths remain visible as relationship information. Overdue snapshots are also marked stale. Loading and empty-device states are explicit. Missing numeric fields render as unavailable; valid zeros are retained.

### Adding devices and current limits

For another climate or PIR device already collected into PostgreSQL, extend the `TopologyService` deployment registry in `api.py` with a unique map ID and climate descriptor or `MotionConfig`. Registered devices are queried together and rendered through the same arrays and branch geometry. The current collector still handles one ESP32 and one PIR; collection/configuration of additional devices remains separate work.

For an **Aqara Water Leak Sensor**, after pairing it with Zigbee2MQTT:

1. Confirm its real topic, identifier and exposed payload fields, then add collector/storage support for its actual `water_leak`, battery and report data.
2. Add a small backend adapter/registry entry returning a `zigbee_water_leak` node and Zigbee link, with real metric/detail descriptors and an optional human-readable `activity_label` such as Water detected. Leave availability unknown until trustworthy reporting exists.
3. Add mapping tests and a browser fixture. The water icon, generic details and grouped layout already work; individual HTML or SVG coordinates are unnecessary.

No water-leak detection/control is implemented by preparing its icon. Automatic inventory/discovery, Zigbee bridge/device availability and mesh routing are not implemented. Coordinator presence is only a device-path check. Existing climate APIs remain centered on one room. RSSI motion monitoring is a separate experiment; the experimental CSI feed is shown in the main Motion card only.

### Map validation

The complete backend suite passes **131 tests**, including existing isolated PostgreSQL checks. Topology coverage includes JSON structure and aggregate status, FastAPI diagnostics, hub/ESP32 mapping, freshness boundaries, PIR state vs availability, evidence-based links, isolated failures, caching, query bounds/cleanup, additional devices, route preservation and the separate HTML page.

`tests/check_topology_browser.py` uses intercepted local fixtures with optional Playwright/Chrome. It passes at 1920×1080, 1440×900, 1024×900, 1000×900, 900×900, 768×1024, 600×900, 390×844 and 320×844 in both themes, checking floating-node styling, neutral rounded SVG paths, hub/label/icon attachment points, panel placement and centered reflow, keyboard/focus, live detail updates, persistent nodes, motion/clear/zero values, reduced motion, expiry, partial/error/recovery/empty states, extra nodes in both protocol groups, long values, safe text, navigation, saved theme and map-only polling. `tests/check_motion_browser.py` also passes at four widths in both themes, including existing chart controls. Screenshots were visually reviewed at 1920×1080, 1440×900, 768×1024 and 390×844 with the drawer open/closed. Mobile protocol stacking and an overly tight first branch were corrected during verification. Fixtures are never published to MQTT or sent to Telegram; browser tooling is not a runtime dependency.

## Adding rooms and modules

To add a module, add its body template, one `MODULES` entry, and a `data-module` host in the appropriate region. The shared card shell, field grids and responsive rules apply automatically. Boiler, garage, garden, solar, battery and UPS cards can use this same path without changing the page grid.

Sensor metadata belongs in `SENSORS`; layout and statistics operate on that configuration. Additional room sections can reuse the sensor grid and templates with unique room-scoped IDs and a corresponding data adapter. The present API returns the latest reading across the database, so multi-room selection will also require room/device filtering in a future backend change. This refactor does not pretend that capability already exists.

## Validation

Headless Chrome checked real API response fixtures at 1440, 1280, 1024, 768, 601, 600, 390 and 320px, in both themes: equal sensor sizes, correct reading order, sidebar alignment, and no horizontal overflow. Functional checks covered metric/range selection, out-of-order range responses, null and zero readings, stale/empty/error/recovery states, CSV export, fullscreen, persistent theme and JavaScript exceptions. Polling registration was verified against the unchanged intervals above.
