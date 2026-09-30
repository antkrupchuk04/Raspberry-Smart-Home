'use strict';

// This page loads only topology data. The dashboard keeps its own polling loop.
const topologyBoard = new SystemTopology(document.getElementById('topology'), { onStatus: updatePageStatus });
const themeToggle = document.getElementById('theme-toggle');

function setTheme(theme) {
  document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
  try { localStorage.setItem('theme', document.documentElement.dataset.theme); } catch (_) { /* Storage may be unavailable. */ }
  const isLight = document.documentElement.dataset.theme === 'light';
  topologyBoard.text(themeToggle, isLight ? '◐ Dark' : '☀ Light');
  themeToggle.setAttribute('aria-label', `Switch to ${isLight ? 'dark' : 'light'} theme`);
}

function updatePageStatus({ snapshot, stale }) {
  const hub = snapshot?.nodes.find(node => node.type === 'hub');
  const label = stale ? 'UNAVAILABLE' : (hub?.status_label || 'Unknown').toUpperCase();
  const state = stale ? 'unknown' : topologyBoard.color(hub?.status);
  const badge = document.getElementById('connection-badge');
  topologyBoard.text(badge, stale ? 'API: unavailable' : 'API: connected');
  topologyBoard.state(badge, stale ? 'error' : 'online');
  topologyBoard.text(document.getElementById('overall-status-text'), label);
  topologyBoard.state(document.getElementById('overall-status'), state === 'error' ? 'offline' : state);
  const uptime = hub?.details.find(field => field.key === 'uptime')?.value;
  topologyBoard.text(document.getElementById('system-uptime'), stale ? null : uptime);
}

function updateClock() {
  topologyBoard.text(document.getElementById('local-clock'), new Date().toLocaleTimeString());
  topologyBoard.tick();
}

themeToggle.addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'));
setTheme(document.documentElement.dataset.theme || 'dark');
updateClock();
topologyBoard.start();
setInterval(updateClock, 1000);
