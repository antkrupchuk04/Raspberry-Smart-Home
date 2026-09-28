'use strict';

// Configuration adds sensors and modules without duplicating the card shell.
const SENSORS = [
  { key: 'temperature', label: 'Temperature', sensor: 'BMP280', interface: 'I²C · 0x76', unit: '°C', precision: 2, accent: 'orange' },
  { key: 'humidity', label: 'Humidity', sensor: 'DHT11', interface: 'GPIO 4', unit: '%', precision: 1, accent: 'green' },
  { key: 'pressure', label: 'Pressure', sensor: 'BMP280', interface: 'I²C · 0x76', unit: 'hPa', precision: 2, accent: 'blue' }
];
const MODULES = {
  history: { title: 'Temperature History', subtitle: 'Room 1 · Sensor history', badge: 'BMP280 · esp32-room1', className: 'history-card', titleId: 'chart-title', badgeId: 'chart-sensor', footerId: 'history-status', footer: 'Loading history…' },
  weather: { title: 'Outdoor Weather', accent: 'blue', badge: 'Loading', badgeId: 'weather-status' },
  ai: { title: 'AI Room Analysis', badge: 'Collecting data', badgeId: 'ai-anomaly' },
  system: { title: 'System Status', subtitle: 'Raspberry Pi 5', badge: 'Connecting', badgeId: 'system-summary', footerId: 'system-updated', footer: 'Loading system health…' },
  motion: { title: 'Motion', subtitle: 'PIR · Zigbee', badge: 'Connecting', badgeId: 'motion-badge', accent: 'green', footerId: 'motion-updated', footer: 'Loading motion history…' },
  automation: { title: 'Automation', subtitle: 'Rules, modes & notifications', badge: 'Planned' },
  camera: { title: 'Camera', subtitle: 'Devices · Snapshot monitoring', badge: 'Planned' },
  zigbee: { title: 'Zigbee Devices', subtitle: 'Devices · Wireless network', badge: 'Connecting', badgeId: 'zigbee-badge' }
};
const byId = id => document.getElementById(id);
const cloneTemplate = id => byId(id).content.cloneNode(true);
function setText(target, value) {
  const node = typeof target === 'string' ? byId(target) : target;
  const text = String(value);
  if (node && node.textContent !== text) node.textContent = text;
}
function setState(id, text, state) {
  const node = byId(id);
  setText(node, text);
  if (node.dataset.state !== state) node.dataset.state = state;
}
function numberOrNull(value) {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
}

function createCard(id, config, bodyTemplate) {
  const card = cloneTemplate('card-template').firstElementChild;
  card.id = id;
  if (config.className) card.classList.add(config.className);
  if (config.accent) card.dataset.accent = config.accent;
  const title = card.querySelector('.card-title');
  title.id = config.titleId || `${id}-title`;
  title.textContent = config.title;
  card.setAttribute('aria-labelledby', title.id);
  const subtitle = card.querySelector('.card-subtitle');
  if (config.subtitle) subtitle.textContent = config.subtitle;
  else subtitle.remove();
  const badge = card.querySelector('.card-badge');
  badge.textContent = config.badge;
  if (config.badgeId) badge.id = config.badgeId;
  card.querySelector('.card-body').append(cloneTemplate(bodyTemplate));
  const footer = card.querySelector('.card-footer');
  if (config.footer) footer.textContent = config.footer;
  if (config.footerId) footer.id = config.footerId;
  return card;
}

const sensorNodes = new Map();
for (const sensor of SENSORS) {
  const card = createCard(`sensor-${sensor.key}`, {
    title: sensor.label, subtitle: `${sensor.sensor} · ${sensor.interface}`,
    badge: sensor.sensor, accent: sensor.accent, className: 'sensor-card'
  }, 'sensor-body');
  card.querySelector('.card-footer').append(cloneTemplate('sensor-footer'));
  const fields = Object.fromEntries([...card.querySelectorAll('[data-field]')].map(node => [node.dataset.field, node]));
  fields.unit.textContent = sensor.unit;
  fields.trend.title = 'Change per hour over the selected history range';
  card.querySelector('.sensor-stats').title = 'Statistics for the selected history range';
  sensorNodes.set(sensor.key, fields);
  byId('sensor-grid').append(card);
}
for (const host of document.querySelectorAll('[data-module]')) {
  const key = host.dataset.module;
  host.append(createCard(key, MODULES[key], `${key}-body`));
}
const weatherFooter = byId('weather').querySelector('.card-footer');
for (const [id, value] of [['weather-condition', 'Weather unavailable'], ['weather-updated', 'Updated —']]) {
  weatherFooter.append(Object.assign(document.createElement('span'), { id, textContent: value }));
}

const root = document.documentElement;
const themeToggle = byId('theme-toggle');
const canvas = byId('tempCanvas');
const ctx = canvas.getContext('2d');
const tooltip = byId('tooltip');
const ttTime = byId('tt-time');
const ttVal = byId('tt-val');
const CHART_METRICS = Object.fromEntries(SENSORS.map(sensor => [sensor.key, {
  ...sensor, sensor: `${sensor.sensor} · esp32-room1`, color: `var(--${sensor.accent})`
}]));
let activeHours = 1;
let activeMetric = 'temperature';
let historyData = [];
let historySignature = null;
let hoveredPoint = null;
let latestPending = false;
let historyController = null;
let lastReadingTime = null;
let telemetryReachable = false;
let systemSnapshot = null;
let systemReceivedAt = null;
let systemPending = false;
let systemRequestFailed = false;
let motionPending = false;
let motionSignature = null;
const ONLINE_TIMEOUT_MS = 15000;
const LATEST_REFRESH_MS = 5000;
const HISTORY_REFRESH_MS = 10000;
const ANALYTICS_REFRESH_MS = 30000;
const WEATHER_REFRESH_MS = 10 * 60 * 1000;
const SYSTEM_STATUS_REFRESH_MS = 10000;
const SYSTEM_REQUEST_TIMEOUT_MS = 8000;
const MOTION_REFRESH_MS = 5000;
const MOTION_REQUEST_TIMEOUT_MS = 4000;
const SYSTEM_SERVICE_IDS = ['mqtt', 'database', 'internet', 'esp32', 'api', 'zigbee'];
const SYSTEM_STATE_LABELS = {
  online: ['ONLINE', 'online'], connected: ['CONNECTED', 'online'], running: ['RUNNING', 'online'],
  offline: ['OFFLINE', 'offline'], error: ['ERROR', 'error'], unknown: ['UNKNOWN', 'unknown'],
  not_configured: ['NOT CONFIGURED', 'warning'], device_present: ['DEVICE PRESENT', 'online']
};

function clearHover() { hoveredPoint = null; tooltip.style.display = 'none'; }
function setTheme(theme) {
  root.dataset.theme = theme === 'light' ? 'light' : 'dark';
  try { localStorage.setItem('theme', root.dataset.theme); } catch (_) { /* Private mode can deny storage. */ }
  const isLight = root.dataset.theme === 'light';
  setText(themeToggle, isLight ? '◐ Dark' : '☀ Light');
  themeToggle.setAttribute('aria-label', `Switch to ${isLight ? 'dark' : 'light'} theme`);
  renderChart();
}

function updateConnectionState() {
  const age = Date.now() - lastReadingTime;
  const online = telemetryReachable && lastReadingTime !== null && age >= -5000 && age < ONLINE_TIMEOUT_MS;
  setState('room-status', online ? 'ONLINE' : 'OFFLINE', online ? 'online' : 'offline');
  updateSystemSummary();
}

function updateSystemSummary() {
  let overall = systemSnapshot?.overall_status;
  if (systemSnapshot) {
    const esp32 = systemSnapshot.services?.esp32 || {};
    const age = numberOrNull(esp32.age_seconds);
    const timeout = numberOrNull(systemSnapshot.thresholds?.esp32_timeout_seconds);
    const elapsed = (performance.now() - systemReceivedAt) / 1000;
    const expired = esp32.status === 'online' && age !== null && timeout !== null && age + elapsed >= timeout;
    const status = expired ? 'offline' : esp32.status;
    setState('system-esp32', ...(SYSTEM_STATE_LABELS[status] || SYSTEM_STATE_LABELS.unknown));
    if (expired && overall === 'healthy') overall = 'warning';
  }
  const labels = { healthy: ['HEALTHY', 'online'], warning: ['WARNING', 'warning'], degraded: ['DEGRADED', 'error'] };
  const [label, state] = labels[overall] || [systemRequestFailed ? 'UNAVAILABLE' : 'CONNECTING', systemRequestFailed ? 'error' : 'unknown'];
  setState('system-summary', label, state);
  const pill = byId('overall-status');
  const headerState = state === 'error' ? 'offline' : state;
  if (pill.dataset.state !== headerState) pill.dataset.state = headerState;
  setText('overall-status-text', label);
  pill.querySelector('.glow-dot').classList.toggle('pulse', state === 'online');
}

async function loadSystemStatus() {
  if (systemPending) return;
  systemPending = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SYSTEM_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch('/api/system/status', { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data.host || !data.cpu || !data.memory || !data.disk || !data.services || !data.thresholds ||
        !['healthy', 'warning', 'degraded'].includes(data.overall_status)) throw new Error('Incomplete system status');
    systemSnapshot = data;
    systemReceivedAt = performance.now();
    systemRequestFailed = false;
    const metric = (id, value, text, warning, critical = Infinity) => {
      setState(id, value === null ? '—' : text,
        value === null ? 'unknown' : value >= critical ? 'error' : value >= warning ? 'warning' : 'online');
    };
    const cpu = numberOrNull(data.cpu.usage_percent);
    const temperature = numberOrNull(data.cpu.temperature_c);
    metric('system-cpu', cpu, `${cpu?.toFixed(0)} %`, data.thresholds.cpu_warning);
    metric('system-temperature', temperature, `${temperature?.toFixed(1)} °C`, data.thresholds.cpu_temp_warning, data.thresholds.cpu_temp_critical);
    for (const key of ['memory', 'disk']) {
      const usage = numberOrNull(data[key].usage_percent);
      metric(`system-${key}`, usage, `${usage?.toFixed(0)} %`, data.thresholds[`${key}_warning`]);
      const used = numberOrNull(data[key].used_gb);
      const total = numberOrNull(data[key].total_gb);
      const title = used !== null && total !== null ? `${used.toFixed(1)} / ${total.toFixed(1)} GB` : 'Usage unavailable';
      if (byId(`system-${key}`).title !== title) byId(`system-${key}`).title = title;
    }
    for (const id of ['system-uptime', 'system-card-uptime']) setText(id, data.host.uptime || '—');
    setText('system-hostname', data.host.hostname || '—');
    for (const key of SYSTEM_SERVICE_IDS) {
      setState(`system-${key}`, ...(SYSTEM_STATE_LABELS[data.services[key]?.status] || SYSTEM_STATE_LABELS.unknown));
    }
    setState('zigbee-coordinator', ...(SYSTEM_STATE_LABELS[data.services.zigbee?.status] || SYSTEM_STATE_LABELS.unknown));
    const alerts = Array.isArray(data.alerts) ? data.alerts.map(alert => alert.message).join(' · ') : '';
    setText('system-updated', `${alerts ? `${alerts} · ` : ''}Updated ${formatTime(data.generated_at)}`);
    setState('connection-badge', 'API: connected', 'online');
  } catch (_) {
    systemSnapshot = null;
    systemRequestFailed = true;
    for (const key of ['cpu', 'memory', 'disk', 'temperature']) {
      setState(`system-${key}`, '—', 'unknown');
      byId(`system-${key}`).removeAttribute('title');
    }
    for (const key of SYSTEM_SERVICE_IDS) setState(`system-${key}`, 'UNKNOWN', 'unknown');
    setState('zigbee-coordinator', 'UNKNOWN', 'unknown');
    setState('system-api', 'UNAVAILABLE', 'error');
    for (const id of ['system-uptime', 'system-card-uptime']) setText(id, '—');
    setText('system-updated', 'System monitoring unavailable. Retrying automatically.');
  } finally {
    clearTimeout(timer);
    systemPending = false;
    updateSystemSummary();
  }
}

function formatDateTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'
  }) : '—';
}

async function loadMotion() {
  if (motionPending) return;
  motionPending = true;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), MOTION_REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch('/api/motion?hours=24&limit=20', { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (typeof data.configured !== 'boolean' || !data.device || !Array.isArray(data.events)) throw new Error('Incomplete motion status');
    const sensor = data.sensor;
    const occupancy = sensor?.occupancy;
    const state = occupancy === true ? ['MOTION DETECTED', 'warning'] : occupancy === false ? ['CLEAR', 'online'] : ['WAITING FOR REPORT', 'unknown'];
    setState('motion-state', ...(data.configured ? state : ['NOT CONFIGURED', 'unknown']));
    setState('motion-badge', data.configured ? 'ZIGBEE PIR' : 'NOT CONFIGURED', data.configured && sensor ? 'online' : 'unknown');
    setText(byId('motion').querySelector('.card-subtitle'), data.configured ? `${data.device.room} · ${data.device.name}` : 'PIR · Zigbee');
    const message = !data.configured ? 'No motion sensor configured.' : !sensor ? 'Listening for the first live sensor report.' :
      'PIR reports motion and clear states. Quiet periods do not indicate an offline sensor.';
    setText('motion-message', message);
    setText('motion-last', formatDateTime(sensor?.last_motion_at));
    setText('motion-count', data.today_count ?? '—');
    byId('motion-count').title = `Recorded motion detections today${data.day_timezone ? ` (${data.day_timezone})` : ''}; repeated reports are counted once.`;
    setText('motion-seen', formatDateTime(sensor?.last_seen));
    byId('motion-state').title = `State reported ${formatDateTime(sensor?.state_reported_at)}`;
    const battery = numberOrNull(sensor?.battery);
    const batteryText = battery === null ? '—' : `${battery.toFixed(0)} %`;
    const batteryState = sensor?.battery_low === true ? 'warning' : 'unknown';
    const signal = numberOrNull(sensor?.linkquality);
    const signalText = signal === null ? '—' : `${signal.toFixed(0)} / 255`;
    for (const prefix of ['motion', 'zigbee']) {
      setState(`${prefix}-battery`, `${batteryText}${sensor?.battery_low === true ? ' · LOW' : ''}`, batteryState);
      setText(`${prefix}-signal`, signalText);
    }
    setState('zigbee-badge', data.configured ? 'INTEGRATED' : 'NOT CONFIGURED', data.configured ? 'online' : 'unknown');
    setText('zigbee-message', data.configured ? 'Motion reports are recorded through Zigbee2MQTT.' : 'No motion sensor configured.');
    setText('zigbee-sensor', data.configured ? data.device.name : '—');
    setText('zigbee-room', data.configured ? data.device.room : '—');
    setText('zigbee-seen', formatDateTime(sensor?.last_seen));
    const signature = JSON.stringify(data.events);
    if (signature !== motionSignature) {
      const nodes = data.events.map(event => {
        const row = document.createElement('li');
        const time = document.createElement('time');
        time.dateTime = event.recorded_at;
        time.textContent = formatDateTime(event.recorded_at);
        const label = document.createElement('span');
        label.textContent = event.occupancy ? 'Motion detected' : 'Clear';
        label.dataset.state = event.occupancy ? 'warning' : 'online';
        const room = document.createElement('span');
        room.textContent = event.room;
        room.className = 'text-secondary';
        row.append(time, label, room);
        return row;
      });
      byId('motion-events').replaceChildren(...nodes);
      motionSignature = signature;
    }
    byId('motion-empty').hidden = data.events.length > 0;
    setText('motion-empty', data.configured ? 'No recorded state changes in the last 24 hours.' : 'Configure a motion sensor to record events.');
    setText('motion-updated', `Latest ${data.events.length} recorded changes · Updated ${formatTime(data.generated_at)}`);
    delete byId('motion').dataset.stale;
  } catch (_) {
    setState('motion-badge', 'UNAVAILABLE', 'error');
    setState('motion-state', 'UNKNOWN', 'unknown');
    setState('zigbee-badge', 'UNAVAILABLE', 'error');
    setText('motion-message', 'Motion monitoring unavailable. Historical values below may be outdated. Retrying automatically.');
    setText('motion-updated', 'Could not refresh motion events.');
    setText('zigbee-message', 'Motion reports unavailable. Retrying automatically.');
    byId('motion').dataset.stale = 'true';
  } finally {
    clearTimeout(timer);
    motionPending = false;
  }
}

async function loadLatest() {
  if (latestPending) return;
  latestPending = true;
  try {
    const response = await fetch('/api/sensors/latest', { cache: 'no-store' });
    setState('connection-badge', 'API: connected', 'online');
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    setText('device-id', data.device_id || '—');
    setText('node-last-sync', formatTime(data.recorded_at));
    const timestamp = data.recorded_at ? new Date(data.recorded_at).getTime() : NaN;
    lastReadingTime = Number.isFinite(timestamp) ? timestamp : null;
    telemetryReachable = true;
    for (const sensor of SENSORS) {
      const fields = sensorNodes.get(sensor.key);
      const value = numberOrNull(data[sensor.key]);
      setText(fields.value, value === null ? '—' : value.toFixed(sensor.precision));
      setText(fields.updated, value === null ? '—' : formatTime(data.recorded_at));
    }
  } catch (error) {
    telemetryReachable = false;
    if (error instanceof TypeError || error instanceof SyntaxError) {
      setState('connection-badge', 'API: unavailable', 'error');
    }
    // Values remain visible with their timestamps, but are clearly marked stale.
    for (const sensor of SENSORS) byId(`sensor-${sensor.key}`).dataset.stale = 'true';
  } finally {
    if (telemetryReachable) for (const sensor of SENSORS) delete byId(`sensor-${sensor.key}`).dataset.stale;
    latestPending = false;
    updateConnectionState();
  }
}

async function loadHistory(force = false) {
  if (historyController) {
    if (!force) return;
    historyController.abort();
  }
  const controller = new AbortController();
  historyController = controller;
  const hours = activeHours;
  try {
    const response = await fetch(`/api/sensors/history?hours=${hours}`, { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (controller !== historyController) return;
    const rows = Array.isArray(payload) ? payload : payload.data || payload.readings || [];
    const data = rows.filter(row => row.recorded_at && Number.isFinite(new Date(row.recorded_at).getTime()))
      .sort((a, b) => new Date(a.recorded_at) - new Date(b.recorded_at));
    const signature = JSON.stringify([hours, data]);
    if (signature !== historySignature) {
      historySignature = signature;
      historyData = data;
      clearHover();
      updateSensorStats();
      renderChart();
    }
    setText('history-status', `${historyData.length} readings · Last ${hours}H · Updated ${formatTime(new Date())}`);
    byId('export-csv').disabled = !historyData.length;
  } catch (error) {
    if (error.name === 'AbortError' || controller !== historyController) return;
    historyData = [];
    historySignature = null;
    clearHover();
    updateSensorStats();
    renderChart();
    setText('history-status', 'History temporarily unavailable. Retrying automatically.');
    byId('export-csv').disabled = true;
  } finally {
    if (controller === historyController) historyController = null;
  }
}

function updateSensorStats() {
  for (const sensor of SENSORS) {
    const fields = sensorNodes.get(sensor.key);
    const rows = historyData.filter(row => numberOrNull(row[sensor.key]) !== null);
    if (!rows.length) {
      for (const field of ['min', 'max', 'avg', 'trend']) setText(fields[field], '—');
      continue;
    }
    const values = rows.map(row => Number(row[sensor.key]));
    const unit = sensor.unit;
    const stats = { min: Math.min(...values), max: Math.max(...values), avg: values.reduce((sum, value) => sum + value, 0) / values.length };
    for (const [field, value] of Object.entries(stats)) setText(fields[field], `${value.toFixed(sensor.precision)} ${unit}`);
    const elapsedHours = (new Date(rows[rows.length - 1].recorded_at) - new Date(rows[0].recorded_at)) / 3600000;
    const slope = elapsedHours > 0 ? (values[values.length - 1] - values[0]) / elapsedHours : null;
    setText(fields.trend, slope === null ? '—' : `${slope >= 0 ? '+' : ''}${slope.toFixed(sensor.precision)} ${unit}/h`);
  }
}

function setMessages(messages) {
  const list = byId('ai-messages');
  const signature = JSON.stringify(messages);
  if (list.dataset.content === signature) return;
  list.dataset.content = signature;
  list.replaceChildren(...messages.map(message => Object.assign(document.createElement('li'), { textContent: String(message) })));
}
async function loadAnalytics() {
  try {
    const response = await fetch('/api/analytics/room', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const comfort = data.comfort || {};
    const state = ['excellent', 'good', 'warning', 'poor', 'collecting'].includes(comfort.status) ? comfort.status : 'collecting';
    setText('ai-status', state.toUpperCase());
    byId('ai-status').className = `ai-status mono ${state}`;
    setText('ai-score', numberOrNull(comfort.score) ?? '—');
    setMessages(Array.isArray(comfort.messages) ? comfort.messages : ['Collecting data for analysis…']);
    const anomaly = data.anomaly || {};
    setText('ai-anomaly', !anomaly.available ? 'Insufficient history' : anomaly.anomaly ? 'Anomaly detected' : 'No anomalies');
    const forecast = data.forecast || {};
    const predicted = numberOrNull(forecast.predicted_temperature);
    setText('ai-forecast', forecast.available && predicted !== null ? `${predicted.toFixed(2)} °C` : '—');
    const trend = data.temperature_trend || {};
    const slope = numberOrNull(trend.slope_c_per_hour);
    setText('ai-trend', trend.available && slope !== null ? `Trend: ${trend.direction} (${slope.toFixed(2)} °C/h)` : 'Trend: collecting data');
  } catch (_) {
    setText('ai-status', 'UNAVAILABLE');
    byId('ai-status').className = 'ai-status mono error';
    for (const id of ['ai-score', 'ai-forecast']) setText(id, '—');
    setText('ai-anomaly', 'API unavailable');
    setText('ai-trend', 'Trend: unavailable');
    setMessages(['AI analysis is temporarily unavailable.']);
  }
}

function weatherIconForCode(code) {
  if (code === 0) return '☀️';
  if (code === 1) return '🌤️';
  if (code === 2) return '⛅';
  if (code === 3) return '☁️';
  if ([45, 48].includes(code)) return '🌫️';
  if ([51, 53, 55, 61, 63, 65, 66, 67, 80, 81, 82].includes(code)) return '🌧️';
  if ([71, 73, 75, 77, 85, 86].includes(code)) return '❄️';
  if ([95, 96, 99].includes(code)) return '⛈️';
  return '•';
}
async function loadWeather() {
  try {
    const response = await fetch('/api/weather/current', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const weather = await response.json();
    setText('weather-status', weather.stale ? 'Cached' : 'Live');
    setText('weather-location', weather.location || 'Outdoor weather');
    setText('weather-icon', weatherIconForCode(numberOrNull(weather.weather_code)));
    const format = (value, precision, unit = '') => numberOrNull(value) === null ? '—' : `${Number(value).toFixed(precision)}${unit}`;
    setText('weather-temp', format(weather.temperature, 1));
    setText('weather-feels', `Feels like ${format(weather.apparent_temperature, 1, ' °C')}`);
    setText('weather-humidity', format(weather.humidity, 0, ' %'));
    setText('weather-pressure', format(weather.pressure, 0, ' hPa'));
    setText('weather-wind', format(weather.wind_speed, 1, ' km/h'));
    setText('weather-rain', format(weather.precipitation, 1, ' mm'));
    setText('weather-condition', weather.condition || 'Unknown');
    setText('weather-updated', `Updated ${formatTime(weather.weather_time)}`);
  } catch (_) {
    setText('weather-status', 'Unavailable');
    setText('weather-location', 'Outdoor weather');
    for (const id of ['weather-icon', 'weather-temp', 'weather-humidity', 'weather-pressure', 'weather-wind', 'weather-rain']) setText(id, '—');
    setText('weather-feels', 'Weather unavailable');
    setText('weather-condition', 'Weather unavailable');
    setText('weather-updated', 'Updated —');
  }
}

function resizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * dpr));
  const height = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width === width && canvas.height === height) return;
  canvas.width = width;
  canvas.height = height;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  clearHover();
  renderChart();
}

function setChartRange(button) {
  const hours = Number(button.dataset.hours);
  if (hours === activeHours) return;
  activeHours = hours;
  for (const item of document.querySelectorAll('.chart-range-btn')) {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  }
  historyData = [];
  historySignature = null;
  clearHover();
  renderChart();
  updateSensorStats();
  setText('history-status', `Loading ${activeHours}H history…`);
  byId('export-csv').disabled = true;
  loadHistory(true);
}
function setChartMetric(button) {
  const metric = button.dataset.metric;
  if (metric === activeMetric) return;
  activeMetric = metric;
  for (const item of document.querySelectorAll('.chart-metric-btn')) {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  }
  const config = CHART_METRICS[metric];
  setText('chart-title', `${config.label} History`);
  setText('chart-sensor', config.sensor);
  byId('chart-sensor').style.color = config.color;
  canvas.setAttribute('aria-label', `${config.label} history chart`);
  clearHover();
  renderChart();
}

function exportHistory() {
  if (!historyData.length) return;
  const rows = [['recorded_at', 'temperature_c', 'humidity_percent', 'pressure_hpa'], ...historyData.map(row => [
    new Date(row.recorded_at).toISOString(), ...SENSORS.map(sensor => numberOrNull(row[sensor.key]) ?? '')
  ])];
  const url = URL.createObjectURL(new Blob([rows.map(row => row.join(',')).join('\r\n')], { type: 'text/csv;charset=utf-8' }));
  const link = Object.assign(document.createElement('a'), { href: url, download: `room1-history-${activeHours}h.csv` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function updateFullscreenControl() {
  const expanded = document.fullscreenElement === byId('history') || byId('history').classList.contains('is-expanded');
  setText('chart-fullscreen', expanded ? 'Exit fullscreen' : 'Fullscreen');
  byId('chart-fullscreen').setAttribute('aria-pressed', String(expanded));
  resizeCanvas();
}
async function toggleFullscreen() {
  const card = byId('history');
  if (document.fullscreenElement === card) await document.exitFullscreen();
  else if (card.classList.contains('is-expanded')) {
    card.classList.remove('is-expanded');
    document.body.classList.remove('chart-expanded');
    byId('chart-fullscreen').focus();
  } else {
    try {
      if (!card.requestFullscreen) throw new Error('Fullscreen unavailable');
      await card.requestFullscreen();
    } catch (_) {
      card.classList.add('is-expanded');
      document.body.classList.add('chart-expanded');
    }
  }
  updateFullscreenControl();
}

// The original standalone canvas renderer follows below.

    function renderChart() {
      const rect = canvas.getBoundingClientRect();
      const width = rect.width;
      const height = rect.height;

      ctx.clearRect(0, 0, width, height);

      const metric = CHART_METRICS[activeMetric];
      const chartData = historyData.filter(row => numberOrNull(row[activeMetric]) !== null);
      if (!chartData.length) {
        ctx.fillStyle = getComputedStyle(root).getPropertyValue('--chart-text').trim();
        ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(`No ${metric.label.toLowerCase()} history available`, width / 2, height / 2);
        canvas._pts = [];
        return;
      }

      const paddingLeft = activeMetric === 'pressure' ? 88 : 68;
      const paddingRight = 20;
      const paddingTop = 20;
      const paddingBottom = 32;
      const plotW = width - paddingLeft - paddingRight;
      const plotH = height - paddingTop - paddingBottom;

      const values = chartData.map(row => Number(row[activeMetric]));
      let minY = Math.min(...values);
      let maxY = Math.max(...values);

      if (minY === maxY) {
        minY -= 1;
        maxY += 1;
      } else {
        const margin = Math.max((maxY - minY) * 0.15, 0.5);
        minY -= margin;
        maxY += margin;
      }

      const colors = getComputedStyle(root);
      const color = name => colors.getPropertyValue(name).trim();
      ctx.strokeStyle = color('--chart-grid');
      ctx.lineWidth = 1;
      ctx.fillStyle = color('--chart-text');
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';

      const gridSteps = 4;
      for (let i = 0; i <= gridSteps; i++) {
        const ratio = i / gridSteps;
        const y = paddingTop + plotH * (1 - ratio);
        const label = minY + (maxY - minY) * ratio;

        ctx.beginPath();
        ctx.setLineDash([4, 4]);
        ctx.moveTo(paddingLeft, y);
        ctx.lineTo(width - paddingRight, y);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillText(`${label.toFixed(1)}${metric.unit}`, paddingLeft - 8, y);
      }

      const firstTime = new Date(chartData[0].recorded_at).getTime();
      const lastTime = new Date(chartData[chartData.length - 1].recorded_at).getTime();
      const timeSpan = Math.max(1, lastTime - firstTime);
      const pts = chartData.map(row => {
        const timestamp = new Date(row.recorded_at).getTime();
        const x = timeSpan === 1
          ? paddingLeft + plotW / 2
          : paddingLeft + ((timestamp - firstTime) / timeSpan) * plotW;

        const val = Number(row[activeMetric]);
        const normY = (val - minY) / (maxY - minY);
        const y = paddingTop + plotH * (1 - normY);

        return {
          x,
          y,
          val,
          time: row.recorded_at
        };
      });

      const gradient = ctx.createLinearGradient(
        0, paddingTop, 0, height - paddingBottom
      );
      gradient.addColorStop(0, color('--chart-fill'));
      gradient.addColorStop(1, color('--chart-fill-end'));

      if (pts.length > 1) {
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y);
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i].x, pts[i].y);
        }
        ctx.lineTo(pts[pts.length - 1].x, height - paddingBottom);
        ctx.lineTo(pts[0].x, height - paddingBottom);
        ctx.closePath();
        ctx.fillStyle = gradient;
        ctx.fill();

        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y);
        for (let i = 1; i < pts.length; i++) {
          ctx.lineTo(pts[i].x, pts[i].y);
        }
        ctx.strokeStyle = color('--chart-line');
        ctx.lineWidth = 2.5;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.stroke();
      }

      const last = pts[pts.length - 1];
      ctx.beginPath();
      ctx.arc(last.x, last.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = color('--chart-line');
      ctx.fill();
      ctx.strokeStyle = color('--chart-point-stroke');
      ctx.lineWidth = 2;
      ctx.stroke();

      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = color('--chart-text');
      ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace';

      const labelCount = Math.min(5, pts.length);
      for (let n = 0; n < labelCount; n++) {
        const idx = labelCount === 1
          ? 0
          : Math.round(n * (pts.length - 1) / (labelCount - 1));
        const p = pts[idx];
        ctx.fillText(formatTime(p.time).slice(0, 5), p.x, height - paddingBottom + 8);
      }

      if (hoveredPoint) {
        ctx.beginPath();
        ctx.arc(hoveredPoint.x, hoveredPoint.y, 5, 0, Math.PI * 2);
        ctx.fillStyle = color('--chart-line');
        ctx.fill();
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      canvas._pts = pts;
    }


    canvas.addEventListener('mousemove', event => {
      const rect = canvas.getBoundingClientRect();
      const mouseX = event.clientX - rect.left;
      const pts = canvas._pts || [];

      let nearest = null;
      let minDistance = 24;

      for (const p of pts) {
        const distance = Math.abs(p.x - mouseX);
        if (distance < minDistance) {
          minDistance = distance;
          nearest = p;
        }
      }

      if (!nearest) {
        hoveredPoint = null;
        tooltip.style.display = 'none';
        renderChart();
        return;
      }

      hoveredPoint = nearest;
      renderChart();

      tooltip.style.display = 'block';
      tooltip.style.left =
        Math.min(rect.width - 140, Math.max(10, nearest.x - 65)) + 'px';
      tooltip.style.top = Math.max(10, nearest.y - 52) + 'px';

      ttTime.textContent =
        new Date(nearest.time).toLocaleString();
      ttVal.textContent = `${nearest.val.toFixed(2)} ${CHART_METRICS[activeMetric].unit}`;
    });

    canvas.addEventListener('mouseleave', () => {
      hoveredPoint = null;
      tooltip.style.display = 'none';
      renderChart();
    });

themeToggle.addEventListener('click', () => setTheme(root.dataset.theme === 'light' ? 'dark' : 'light'));
document.querySelectorAll('.chart-metric-btn').forEach(button => button.addEventListener('click', () => setChartMetric(button)));
document.querySelectorAll('.chart-range-btn').forEach(button => button.addEventListener('click', () => setChartRange(button)));
byId('export-csv').addEventListener('click', exportHistory);
byId('chart-fullscreen').addEventListener('click', toggleFullscreen);
document.addEventListener('fullscreenchange', updateFullscreenControl);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && byId('history').classList.contains('is-expanded')) toggleFullscreen();
});
new ResizeObserver(resizeCanvas).observe(byId('chartWrapper'));
window.addEventListener('resize', resizeCanvas);

setTheme(root.dataset.theme || 'dark');
resizeCanvas();
loadLatest();
loadHistory();
loadAnalytics();
loadWeather();
loadSystemStatus();
loadMotion();
const updateClock = () => {
  setText('local-clock', new Date().toLocaleTimeString());
  updateConnectionState();
};
setText('local-clock', new Date().toLocaleTimeString());
// Preserve network cadence. The local clock also expires stale online status.
setInterval(loadLatest, LATEST_REFRESH_MS);
setInterval(loadHistory, HISTORY_REFRESH_MS);
setInterval(loadAnalytics, ANALYTICS_REFRESH_MS);
setInterval(loadWeather, WEATHER_REFRESH_MS);
setInterval(loadSystemStatus, SYSTEM_STATUS_REFRESH_MS);
setInterval(loadMotion, MOTION_REFRESH_MS);
setInterval(updateClock, 1000);
