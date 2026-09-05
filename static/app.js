const ONLINE_TIMEOUT_SECONDS = 15;
const LATEST_REFRESH_MS = 5000;
const HISTORY_REFRESH_MS = 10000;

let selectedHours = 1;
let latestReading = null;
let historyRows = [];
let latestTimer = null;
let historyTimer = null;

function normalizeLatest(payload) {
  if (!payload) return null;
  if (Array.isArray(payload)) return payload[0] ?? null;
  if (payload.data && typeof payload.data === "object") return payload.data;
  return payload;
}

function normalizeHistory(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.data)) return payload.data;
  if (payload && Array.isArray(payload.readings)) return payload.readings;
  return [];
}

function asNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function parseTimestamp(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatClock(date) {
  if (!date) return "—";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(date);
}

function formatShortTimestamp(date) {
  if (!date) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function readingAgeSeconds(reading) {
  const timestamp = parseTimestamp(reading?.recorded_at);
  if (!timestamp) return Infinity;
  return Math.max(0, (Date.now() - timestamp.getTime()) / 1000);
}

function setOnlineState(online) {
  const system = document.getElementById("system-status");
  const systemText = document.getElementById("system-status-text");
  const device = document.getElementById("device-status");
  const deviceText = document.getElementById("device-status-text");

  system.classList.toggle("online", online);
  system.classList.toggle("offline", !online);
  systemText.textContent = online ? "ONLINE" : "OFFLINE";

  device.classList.toggle("online", online);
  device.classList.toggle("offline", !online);
  deviceText.textContent = online ? "Status: ONLINE" : "Status: OFFLINE";
}

function updateLatestUI(reading) {
  latestReading = reading;

  if (!reading) {
    setOnlineState(false);
    return;
  }

  const temperature = asNumber(reading.temperature);
  const pressure = asNumber(reading.pressure);
  const timestamp = parseTimestamp(reading.recorded_at);
  const age = readingAgeSeconds(reading);

  document.getElementById("card-temp-value").textContent =
    temperature === null ? "—" : temperature.toFixed(2);

  document.getElementById("card-pressure-value").textContent =
    pressure === null ? "—" : pressure.toFixed(2);

  document.getElementById("node-last-sync").textContent =
    timestamp ? formatClock(timestamp) : "—";

  document.getElementById("pressure-age").textContent =
    Number.isFinite(age) ? `${Math.round(age)} s ago` : "—";

  setOnlineState(age < ONLINE_TIMEOUT_SECONDS);
  updatePressureBar(pressure);
  updateMetricStats();
}

function updatePressureBar(pressure) {
  const fill = document.getElementById("pressure-bar-fill");
  if (pressure === null) {
    fill.style.width = "0%";
    return;
  }

  // Merely a visual position within a broad normal atmospheric range.
  const min = 930;
  const max = 1050;
  const ratio = Math.max(0, Math.min(1, (pressure - min) / (max - min)));
  fill.style.width = `${ratio * 100}%`;
}

async function fetchLatest() {
  try {
    const response = await fetch("/api/sensors/latest", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    updateLatestUI(normalizeLatest(await response.json()));
  } catch (error) {
    console.error("Failed to fetch latest telemetry:", error);
    setOnlineState(false);
  }
}

async function fetchHistory() {
  try {
    const response = await fetch(`/api/sensors/history?hours=${selectedHours}`, {
      cache: "no-store"
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    historyRows = normalizeHistory(await response.json())
      .filter(row => asNumber(row.temperature) !== null && parseTimestamp(row.recorded_at))
      .sort((a, b) => parseTimestamp(a.recorded_at) - parseTimestamp(b.recorded_at));

    drawTemperatureChart();
    drawSparkline();
    updateMetricStats();
  } catch (error) {
    console.error("Failed to fetch history:", error);
    historyRows = [];
    drawTemperatureChart();
    drawSparkline();
  }
}

function updateMetricStats() {
  const values = historyRows
    .map(row => asNumber(row.temperature))
    .filter(v => v !== null);

  const minEl = document.getElementById("temp-min");
  const maxEl = document.getElementById("temp-max");
  const avgEl = document.getElementById("temp-avg");
  const trendEl = document.getElementById("temp-trend");
  const pressureTrendEl = document.getElementById("pressure-trend");

  if (!values.length) {
    minEl.textContent = "—";
    maxEl.textContent = "—";
    avgEl.textContent = "—";
    trendEl.className = "trend-chip neutral";
    trendEl.textContent = "Waiting for data";
    pressureTrendEl.className = "trend-chip neutral";
    pressureTrendEl.textContent = "Waiting for data";
    return;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const avg = values.reduce((sum, value) => sum + value, 0) / values.length;

  minEl.textContent = `${min.toFixed(2)}°C`;
  maxEl.textContent = `${max.toFixed(2)}°C`;
  avgEl.textContent = `${avg.toFixed(2)}°C`;

  if (values.length >= 2) {
    const delta = values[values.length - 1] - values[0];
    trendEl.className =
      delta > 0.05 ? "trend-chip positive" :
      delta < -0.05 ? "trend-chip negative" :
      "trend-chip neutral";
    trendEl.textContent =
      `${delta >= 0 ? "+" : ""}${delta.toFixed(2)}°C/${selectedHours}h`;
  }

  const pressures = historyRows
    .map(row => asNumber(row.pressure))
    .filter(v => v !== null);

  if (pressures.length >= 2) {
    const delta = pressures[pressures.length - 1] - pressures[0];
    pressureTrendEl.className =
      Math.abs(delta) < 0.2 ? "trend-chip neutral" :
      delta > 0 ? "trend-chip positive" :
      "trend-chip negative";
    pressureTrendEl.textContent =
      `${delta >= 0 ? "+" : ""}${delta.toFixed(2)} hPa/${selectedHours}h`;
  }
}

function prepareCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();

  if (canvas.width !== Math.round(rect.width * ratio) ||
      canvas.height !== Math.round(rect.height * ratio)) {
    canvas.width = Math.round(rect.width * ratio);
    canvas.height = Math.round(rect.height * ratio);
  }

  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { ctx, width: rect.width, height: rect.height };
}

function drawSparkline() {
  const canvas = document.getElementById("temp-sparkline");
  const { ctx, width, height } = prepareCanvas(canvas);
  ctx.clearRect(0, 0, width, height);

  const values = historyRows.slice(-60)
    .map(row => asNumber(row.temperature))
    .filter(v => v !== null);

  if (values.length < 2) return;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(0.5, max - min);
  const pad = 4;

  ctx.beginPath();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#0284c7";

  values.forEach((value, index) => {
    const x = pad + (index / (values.length - 1)) * (width - pad * 2);
    const y = pad + ((max - value) / range) * (height - pad * 2);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });

  ctx.stroke();
}

function computeChartPoints(width, height) {
  if (!historyRows.length) return null;

  const pad = { left: 48, right: 12, top: 14, bottom: 30 };
  const plotWidth = Math.max(1, width - pad.left - pad.right);
  const plotHeight = Math.max(1, height - pad.top - pad.bottom);

  const values = historyRows.map(r => asNumber(r.temperature)).filter(v => v !== null);
  let min = Math.min(...values);
  let max = Math.max(...values);

  if (Math.abs(max - min) < 0.5) {
    min -= 0.5;
    max += 0.5;
  } else {
    const margin = (max - min) * 0.12;
    min -= margin;
    max += margin;
  }

  const firstTime = parseTimestamp(historyRows[0].recorded_at).getTime();
  const lastTime = parseTimestamp(historyRows[historyRows.length - 1].recorded_at).getTime();
  const timeSpan = Math.max(1, lastTime - firstTime);

  const points = historyRows.map(row => {
    const value = asNumber(row.temperature);
    const time = parseTimestamp(row.recorded_at).getTime();
    return {
      x: pad.left + ((time - firstTime) / timeSpan) * plotWidth,
      y: pad.top + ((max - value) / (max - min)) * plotHeight,
      value,
      date: parseTimestamp(row.recorded_at)
    };
  });

  return { pad, plotWidth, plotHeight, min, max, firstTime, lastTime, points };
}

function drawTemperatureChart() {
  const canvas = document.getElementById("temperature-chart");
  const { ctx, width, height } = prepareCanvas(canvas);
  ctx.clearRect(0, 0, width, height);

  if (!historyRows.length) {
    ctx.fillStyle = "#94a3b8";
    ctx.font = '12px "JetBrains Mono", ui-monospace, monospace';
    ctx.textAlign = "center";
    ctx.fillText("No history data available", width / 2, height / 2);
    return;
  }

  const chart = computeChartPoints(width, height);
  const { pad, plotWidth, plotHeight, min, max, firstTime, lastTime, points } = chart;

  ctx.font = '11px "JetBrains Mono", ui-monospace, monospace';
  ctx.lineWidth = 1;

  // Horizontal grid + Y labels.
  const gridCount = 4;
  for (let i = 0; i <= gridCount; i++) {
    const y = pad.top + (i / gridCount) * plotHeight;
    const value = max - (i / gridCount) * (max - min);

    ctx.strokeStyle = "#e2e8f0";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotWidth, y);
    ctx.stroke();

    ctx.setLineDash([]);
    ctx.fillStyle = "#475569";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(`${value.toFixed(1)}°`, pad.left - 8, y);
  }

  // X labels.
  const xTicks = width < 550 ? 3 : 5;
  for (let i = 0; i < xTicks; i++) {
    const ratio = xTicks === 1 ? 0 : i / (xTicks - 1);
    const x = pad.left + ratio * plotWidth;
    const date = new Date(firstTime + ratio * (lastTime - firstTime));

    ctx.fillStyle = "#475569";
    ctx.textAlign =
      i === 0 ? "left" :
      i === xTicks - 1 ? "right" :
      "center";
    ctx.textBaseline = "top";
    ctx.fillText(formatClock(date).slice(0, 5), x, pad.top + plotHeight + 9);
  }

  // Area fill.
  const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotHeight);
  gradient.addColorStop(0, "rgba(2,132,199,0.15)");
  gradient.addColorStop(1, "rgba(2,132,199,0)");

  ctx.beginPath();
  ctx.moveTo(points[0].x, pad.top + plotHeight);
  points.forEach((p, i) => {
    if (i === 0) ctx.lineTo(p.x, p.y);
    else ctx.lineTo(p.x, p.y);
  });
  ctx.lineTo(points[points.length - 1].x, pad.top + plotHeight);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  // Main line.
  ctx.beginPath();
  points.forEach((p, i) => {
    if (i === 0) ctx.moveTo(p.x, p.y);
    else ctx.lineTo(p.x, p.y);
  });
  ctx.strokeStyle = "#0284c7";
  ctx.lineWidth = 2.2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke();

  // Last point.
  const last = points[points.length - 1];
  ctx.fillStyle = "#0284c7";
  ctx.beginPath();
  ctx.arc(last.x, last.y, 4, 0, Math.PI * 2);
  ctx.fill();

  canvas._chartPoints = points;
}

function handleChartPointer(event) {
  const canvas = document.getElementById("temperature-chart");
  const tooltip = document.getElementById("chart-tooltip");
  const points = canvas._chartPoints || [];

  if (!points.length) {
    tooltip.hidden = true;
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;

  let nearest = points[0];
  let distance = Math.abs(points[0].x - x);

  for (const point of points) {
    const d = Math.abs(point.x - x);
    if (d < distance) {
      nearest = point;
      distance = d;
    }
  }

  if (distance > 35) {
    tooltip.hidden = true;
    return;
  }

  tooltip.innerHTML =
    `<div>${formatShortTimestamp(nearest.date)}</div>` +
    `<strong>${nearest.value.toFixed(2)} °C</strong>`;

  tooltip.style.left = `${Math.min(nearest.x, rect.width - 135)}px`;
  tooltip.style.top = `${Math.max(20, Math.min(nearest.y, rect.height - 20))}px`;
  tooltip.hidden = false;
}

function setChartRange(hours) {
  selectedHours = hours;

  document.querySelectorAll(".range-button").forEach(button => {
    button.classList.toggle("active", Number(button.dataset.hours) === hours);
  });

  fetchHistory();
}

function updateClock() {
  const now = new Date();
  document.getElementById("footer-clock").textContent =
    `Local time: ${formatClock(now)}`;

  if (latestReading) {
    setOnlineState(readingAgeSeconds(latestReading) < ONLINE_TIMEOUT_SECONDS);
    document.getElementById("pressure-age").textContent =
      `${Math.round(readingAgeSeconds(latestReading))} s ago`;
  }
}

function refreshAll() {
  fetchLatest();
  fetchHistory();
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".range-button").forEach(button => {
    button.addEventListener("click", () => setChartRange(Number(button.dataset.hours)));
  });

  const pollBtn = document.getElementById("poll-btn");
  pollBtn.addEventListener("click", async () => {
    pollBtn.classList.add("loading");
    await Promise.allSettled([fetchLatest(), fetchHistory()]);
    pollBtn.classList.remove("loading");
  });

  const chart = document.getElementById("temperature-chart");
  chart.addEventListener("mousemove", handleChartPointer);
  chart.addEventListener("mouseleave", () => {
    document.getElementById("chart-tooltip").hidden = true;
  });

  window.addEventListener("resize", () => {
    drawTemperatureChart();
    drawSparkline();
  });

  refreshAll();
  updateClock();

  latestTimer = setInterval(fetchLatest, LATEST_REFRESH_MS);
  historyTimer = setInterval(fetchHistory, HISTORY_REFRESH_MS);
  setInterval(updateClock, 1000);
});
