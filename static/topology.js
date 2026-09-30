'use strict';

// Known icons are local SVG symbols. Unknown types retain the generic renderer.
const MAP_DEVICE_TYPES = {
  hub: { icon: 'hub', label: 'Smart Home Hub' },
  esp32: { icon: 'esp32', label: 'ESP32 climate node' },
  zigbee_motion: { icon: 'motion', label: 'Motion sensor', condition: state => ({ detected: 'Motion detected', clear: 'No motion', unknown: 'State unknown' })[state] },
  zigbee_coordinator: { icon: 'coordinator', label: 'Zigbee coordinator' },
  zigbee_water_leak: { icon: 'water', label: 'Water leak sensor' },
  zigbee_smart_plug: { icon: 'plug', label: 'Smart plug' },
  zigbee_contact: { icon: 'contact', label: 'Door / window sensor' },
  generic_sensor: { icon: 'sensor', label: 'Sensor' }
};

class SystemTopology {
  constructor(root, { onStatus = () => {} } = {}) {
    this.root = root;
    this.onStatus = onStatus;
    this.board = root.querySelector('.topology-board');
    this.workspace = root.querySelector('.smart-map-workspace');
    this.panel = root.querySelector('#map-details');
    this.nodes = new Map();
    this.groups = new Map();
    this.edges = new Map();
    this.views = new Map();
    this.snapshot = null;
    this.selectedId = null;
    this.pending = false;
    this.failed = false;
    this.receivedAt = 0;
    this.refreshMs = 10000;
    this.requestTimeoutMs = 8000;
    this.observer = new ResizeObserver(() => this.scheduleLayout());
    this.observer.observe(this.board);
    root.querySelector('#map-detail-close').addEventListener('click', () => this.closeDetails());
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && this.selectedId) { event.preventDefault(); this.closeDetails(); }
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) { this.tick(); this.load(); }
    });
  }

  text(element, value) {
    const text = value == null ? '—' : String(value);
    if (element.textContent !== text) element.textContent = text;
  }

  attr(element, key, value) {
    if (element.getAttribute(key) !== String(value)) element.setAttribute(key, String(value));
  }

  state(element, value) { this.attr(element, 'data-state', value); }

  color(status) {
    return ({ online: 'online', offline: 'error', degraded: 'error', warning: 'warning', stale: 'warning' })[status] || 'unknown';
  }

  format(field) {
    if (field.value === null || field.value === undefined) return 'Unavailable';
    if (field.kind === 'timestamp') {
      const date = new Date(field.value);
      return Number.isFinite(date.getTime()) ? date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'Unknown';
    }
    if (field.kind === 'number') {
      if (typeof field.value !== 'number' || !Number.isFinite(field.value)) return 'Unavailable';
      return `${field.value.toFixed(Math.max(0, Math.min(2, field.precision || 0)))}${field.unit ? ` ${field.unit}` : ''}`;
    }
    return String(field.value);
  }

  fields(list, fields, stale = false) {
    const signature = fields.map(field => field.key).join('|');
    if (list.dataset.structure !== signature) {
      list.replaceChildren(...fields.map(field => {
        const row = document.createElement('div');
        row.dataset.key = field.key;
        row.append(document.createElement('dt'), document.createElement('dd'));
        return row;
      }));
      list.dataset.structure = signature;
    }
    fields.forEach((field, index) => {
      const row = list.children[index];
      this.text(row.firstElementChild, field.label);
      this.text(row.lastElementChild, this.format(field));
      this.state(row.lastElementChild, stale ? 'unknown' : field.state || 'unknown');
    });
  }

  validate(data) {
    if (!data || !Array.isArray(data.nodes) || !Array.isArray(data.links) || !['ok', 'partial'].includes(data.status)) throw new Error('Invalid topology');
    const ids = new Set();
    for (const node of data.nodes) {
      if (!node || typeof node.id !== 'string' || !node.id || ids.has(node.id) || !Array.isArray(node.metrics) || !Array.isArray(node.details)) throw new Error('Invalid node');
      if ([...node.metrics, ...node.details].some(field => !field || typeof field.key !== 'string')) throw new Error('Invalid field');
      ids.add(node.id);
    }
    const links = new Set();
    for (const edge of data.links) {
      if (!edge || typeof edge.id !== 'string' || links.has(edge.id) || !ids.has(edge.source) || !ids.has(edge.target)) throw new Error('Invalid link');
      links.add(edge.id);
    }
    if (!Number.isFinite(new Date(data.generated_at).getTime())) throw new Error('Invalid timestamp');
  }

  group(key, label) {
    if (!this.groups.has(key)) {
      const section = document.createElement('section');
      section.className = 'map-group';
      section.dataset.transport = key;
      const heading = document.createElement('h3');
      heading.className = 'map-group-title mono';
      const list = document.createElement('div');
      list.className = 'map-device-list';
      section.append(heading, list);
      this.board.querySelector('.map-groups').append(section);
      const trunk = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      this.board.querySelector('.map-trunks').append(trunk);
      this.groups.set(key, { section, heading, list, trunk });
      this.observer.observe(section);
    }
    const group = this.groups.get(key);
    this.text(group.heading, label);
    return group;
  }

  render(data) {
    const oldById = new Map((this.snapshot?.nodes || []).map(node => [node.id, node]));
    const recovered = this.failed;
    this.snapshot = data;
    this.receivedAt = performance.now();
    this.failed = false;
    const keep = new Set();
    for (const node of data.nodes) {
      keep.add(node.id);
      let button = this.nodes.get(node.id);
      if (!button) {
        button = document.getElementById('topology-node').content.firstElementChild.cloneNode(true);
        button.dataset.nodeId = node.id;
        button.addEventListener('click', () => this.select(node.id));
        button.addEventListener('animationend', () => button.classList.remove('map-pulse'));
        this.nodes.set(node.id, button);
        this.observer.observe(button);
      }
      this.attr(button, 'data-type', node.type);
      const type = MAP_DEVICE_TYPES[node.type] || MAP_DEVICE_TYPES.generic_sensor;
      this.attr(button.querySelector('use'), 'href', `#map-icon-${type.icon}`);
      const link = data.links.find(edge => edge.source === node.id || edge.target === node.id);
      let parent = this.board.querySelector('.map-hub');
      if (node.type !== 'hub') {
        const key = link?.transport || link?.protocol || 'other';
        const label = ({ wifi: 'Wi-Fi / MQTT', zigbee: 'Zigbee' })[key] || link?.label || 'Other devices';
        parent = this.group(key, label).list;
      }
      if (button.parentElement !== parent) parent.append(button);
      this.text(button.querySelector('.map-node-name'), node.name);
      this.text(button.querySelector('.map-node-room'), node.type === 'hub' ? 'Smart Home Hub' : node.room || type.label);
      const old = oldById.get(node.id);
      if (!matchMedia('(prefers-reduced-motion: reduce)').matches && !recovered && old && node.activity === 'detected' && node.activity_at && node.activity_at !== old.activity_at && node.age_seconds >= 0 && node.age_seconds < 2 * this.refreshMs / 1000) {
        button.classList.remove('map-pulse');
        requestAnimationFrame(() => button.classList.add('map-pulse'));
      }
    }
    for (const [id, button] of this.nodes) {
      if (!keep.has(id)) { this.observer.unobserve(button); button.remove(); this.nodes.delete(id); }
    }
    for (const [key, group] of this.groups) {
      if (!group.list.childElementCount) { this.observer.unobserve(group.section); group.section.remove(); group.trunk.remove(); this.groups.delete(key); }
      else this.attr(group.section, 'data-multiple', String(group.list.childElementCount > 1));
    }
    if (this.selectedId && !keep.has(this.selectedId)) this.closeDetails();
    const edgeIds = new Set();
    for (const edge of data.links) {
      edgeIds.add(edge.id);
      if (!this.edges.has(edge.id)) {
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        this.board.querySelector('.topology-path-group').append(path);
        this.edges.set(edge.id, path);
      }
    }
    for (const [id, path] of this.edges) {
      if (!edgeIds.has(id)) { path.remove(); this.edges.delete(id); }
    }
    const empty = this.board.querySelector('.map-empty');
    empty.hidden = data.nodes.some(node => node.type !== 'hub');
    this.text(empty, data.nodes.length ? 'No room devices configured yet.' : 'No device information is available.');
    this.text(this.root.querySelector('#topology-device-count'), `${data.nodes.length} ${data.nodes.length === 1 ? 'device' : 'devices'}`);
    this.tick();
    this.scheduleLayout();
  }

  age(seconds) {
    if (typeof seconds !== 'number' || seconds < 0) return 'Unknown';
    if (seconds < 60) return `${Math.floor(seconds)}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }

  tick() {
    if (!this.snapshot) return;
    const elapsed = Math.max(0, (performance.now() - this.receivedAt) / 1000);
    const stale = this.failed || elapsed >= (2 * this.refreshMs + this.requestTimeoutMs) / 1000;
    const partial = this.snapshot.status === 'partial';
    const expiredIds = new Set();
    this.views.clear();
    for (const node of this.snapshot.nodes) {
      const button = this.nodes.get(node.id);
      // Expiry comes from the backend freshness lease, not another timeout rule.
      const expired = node.status === 'online' && typeof node.online_for_seconds === 'number' && elapsed >= node.online_for_seconds;
      if (expired) expiredIds.add(node.id);
      const status = stale ? 'stale' : node.data_status === 'unavailable' ? 'unavailable' : expired ? 'offline' : node.status;
      const label = stale ? 'Stale data' : node.data_status === 'unavailable' ? 'Data unavailable' : expired ? 'Offline' : node.status_label || 'Unknown';
      const type = MAP_DEVICE_TYPES[node.type] || MAP_DEVICE_TYPES.generic_sensor;
      const condition = stale || node.data_status === 'unavailable' ? null : type.condition?.(node.activity) || node.activity_label;
      const age = typeof node.age_seconds === 'number' && node.age_seconds >= 0 ? node.age_seconds + elapsed : null;
      this.views.set(node.id, { status, label, condition, age, stale });
      this.text(button.querySelector('[data-field="status"]'), label);
      this.state(button.querySelector('.map-node-status'), this.color(status));
      this.attr(button, 'data-health', status);
      this.attr(button, 'data-active', String(!stale && node.activity === 'detected'));
      this.attr(button, 'aria-label', `${node.name}${node.room ? `, ${node.room}` : ''}. ${label}${condition ? `. Last reported: ${condition}` : ''}. View details`);
      const activity = button.querySelector('.map-node-condition');
      activity.hidden = !condition;
      this.text(activity, condition || '');
      this.state(activity, node.activity === 'detected' ? 'warning' : 'unknown');
    }
    const hub = this.snapshot.nodes.find(node => node.type === 'hub');
    const systemStatus = this.snapshot.system?.status || ({ online: 'healthy', warning: 'warning', degraded: 'degraded' })[hub?.status] || 'unknown';
    const awaiting = expiredIds.size > 0 && systemStatus === 'healthy';
    const label = stale ? 'Map unavailable' : partial ? 'Partial data' : awaiting ? 'Awaiting update' :
      ({ healthy: 'System healthy', warning: 'Attention required', degraded: 'Attention required', unknown: 'Status unknown' })[systemStatus] || 'Status unknown';
    const summary = this.root.querySelector('#topology-summary');
    this.text(summary, label);
    this.state(summary, stale || partial || awaiting ? 'warning' : ({ healthy: 'online', warning: 'warning', degraded: 'error' })[systemStatus] || 'unknown');
    const message = this.root.querySelector('#topology-message');
    message.hidden = !stale && !partial;
    this.text(message, stale ? 'Live updates are unavailable. Showing older data while we reconnect.' : partial ?
      'Some device information is unavailable. Select a device to see what is known.' : '');
    this.text(this.root.querySelector('#topology-updated'), `Updated ${new Date(this.snapshot.generated_at).toLocaleTimeString()} · Refresh 10s${stale ? ' · Older snapshot' : ''}`);
    // Connections describe topology, not radio/transport health. Node indicators
    // retain the backend's operational status and freshness semantics.
    if (this.selectedId) this.updateDetails();
    this.onStatus({ snapshot: this.snapshot, stale });
  }

  select(id) {
    if (!this.nodes.has(id)) return;
    if (this.selectedId === id) { this.closeDetails(); return; }
    this.selectedId = id;
    this.panel.hidden = false;
    this.workspace.dataset.detailsOpen = 'true';
    for (const [key, button] of this.nodes) {
      this.attr(button, 'aria-expanded', String(key === id));
      this.attr(button, 'data-selected', String(key === id));
    }
    this.updateDetails();
    this.panel.querySelector('h3').focus({ preventScroll: true });
    if (matchMedia('(max-width: 899px)').matches) this.panel.scrollIntoView({ block: 'start', behavior: 'auto' });
    this.scheduleLayout();
  }

  closeDetails() {
    const previous = this.nodes.get(this.selectedId);
    const restore = this.panel.contains(document.activeElement) || !document.activeElement?.isConnected;
    this.selectedId = null;
    this.panel.hidden = true;
    this.workspace.dataset.detailsOpen = 'false';
    for (const button of this.nodes.values()) {
      this.attr(button, 'aria-expanded', 'false');
      this.attr(button, 'data-selected', 'false');
    }
    if (restore) {
      const target = previous || this.board.querySelector('.map-node') || this.root.querySelector('h2');
      target.tabIndex = target.tagName === 'BUTTON' ? 0 : -1;
      target.focus();
    }
    this.scheduleLayout();
  }

  updateDetails() {
    const node = this.snapshot.nodes.find(item => item.id === this.selectedId);
    const view = this.views.get(this.selectedId);
    if (!node || !view) return;
    const type = MAP_DEVICE_TYPES[node.type] || MAP_DEVICE_TYPES.generic_sensor;
    const link = this.snapshot.links.find(edge => edge.source === node.id || edge.target === node.id);
    const plain = (key, label, value) => ({ key, label, value, kind: 'text' });
    this.text(this.panel.querySelector('#map-detail-title'), node.name);
    const status = this.panel.querySelector('#map-detail-status');
    this.text(status, view.label + (view.stale ? ' · Values below may be outdated' : ''));
    this.state(status, this.color(view.status));
    this.fields(this.panel.querySelector('#map-detail-overview'), [
      plain('map_id', 'Map ID', node.id), plain('device_id', 'Device ID', node.device_id),
      plain('device_type', 'Type', type.label), plain('room', node.type === 'hub' ? 'Hostname' : 'Room', node.room),
      plain('connection', 'Connection', node.type === 'hub' ? 'Central hub' : link?.label || 'Unknown'),
      ...(node.type === 'hub' ? [] : [plain('age', 'Last report', this.age(view.age))])
    ]);
    this.text(this.panel.querySelector('#map-readings-title'), node.type === 'hub' ? 'Resources' : 'Readings & state');
    this.fields(this.panel.querySelector('#map-detail-metrics'), node.metrics, view.stale);
    this.fields(this.panel.querySelector('#map-detail-diagnostics'), node.details, view.stale);
    this.text(this.panel.querySelector('#map-detail-note'), [node.note, node.type !== 'hub' && link?.note].filter(Boolean).join(' '));
  }

  scheduleLayout() {
    if (this.layoutFrame) return;
    this.layoutFrame = requestAnimationFrame(() => { this.layoutFrame = null; this.layout(); });
  }

  roundedPath(points) {
    const clean = points.filter((point, index) => !index || point[0] !== points[index - 1][0] || point[1] !== points[index - 1][1]);
    if (!clean.length) return '';
    let d = `M ${clean[0].join(' ')}`;
    for (let index = 1; index < clean.length - 1; index++) {
      const previous = clean[index - 1], corner = clean[index], next = clean[index + 1];
      const before = Math.hypot(corner[0] - previous[0], corner[1] - previous[1]);
      const after = Math.hypot(next[0] - corner[0], next[1] - corner[1]);
      const radius = Math.min(10, before / 2, after / 2);
      const entry = corner.map((value, axis) => value + (previous[axis] - value) * radius / before);
      const exit = corner.map((value, axis) => value + (next[axis] - value) * radius / after);
      d += ` L ${entry.join(' ')} Q ${corner.join(' ')} ${exit.join(' ')}`;
    }
    return clean.length > 1 ? `${d} L ${clean.at(-1).join(' ')}` : d;
  }

  layout() {
    if (!this.snapshot) return;
    const box = this.board.getBoundingClientRect();
    const svg = this.board.querySelector('svg');
    svg.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
    const rect = element => {
      const r = element.getBoundingClientRect();
      return { left: r.left - box.left, right: r.right - box.left, top: r.top - box.top, bottom: r.bottom - box.top, x: (r.left + r.right) / 2 - box.left, y: (r.top + r.bottom) / 2 - box.top };
    };
    const hubNode = this.snapshot.nodes.find(node => node.type === 'hub');
    const hub = hubNode && this.nodes.get(hubNode.id);
    const hubRect = hub ? rect(hub) : null;
    const firstGroup = this.groups.values().next().value;
    const firstTop = firstGroup ? rect(firstGroup.section).top : 0;
    const stacked = [...this.groups.values()].some(group => Math.abs(rect(group.section).top - firstTop) > 1);
    const splitY = hubRect ? hubRect.bottom + 30 : 0;
    // Wrapped groups branch from a shared outside gutter, never from a preceding
    // device. In a single row each protocol connects directly beneath the hub.
    const gutter = 8;
    for (const group of this.groups.values()) {
      const heading = rect(group.heading);
      const points = hubRect ? [[hubRect.x, hubRect.bottom + 8], [hubRect.x, splitY]] : [];
      if (stacked && hubRect && rect(group.section).top > firstTop + 1) points.push([gutter, splitY], [gutter, heading.top - 18], [heading.x, heading.top - 18]);
      else if (hubRect) points.push([heading.x, splitY]);
      if (hubRect) points.push([heading.x, heading.top - 6]);
      let d = this.roundedPath(points);
      if (hubRect && group.list.childElementCount > 1) {
        const lastIcon = rect(group.list.lastElementChild.querySelector('.map-node-icon'));
        const spine = rect(group.section).left + 20;
        d += ' ' + this.roundedPath([[heading.x, heading.bottom + 6], [heading.x, heading.bottom + 18], [spine, heading.bottom + 18], [spine, lastIcon.top - 20]]);
      }
      this.attr(group.trunk, 'd', d);
    }
    for (const edge of this.snapshot.links) {
      const leafId = edge.source === hubNode?.id ? edge.target : edge.target === hubNode?.id ? edge.source : null;
      const leaf = leafId && this.nodes.get(leafId);
      const group = leaf && this.groups.get(edge.transport || edge.protocol || 'other');
      let d = '';
      if (hubRect && leaf && group) {
        const icon = rect(leaf.querySelector('.map-node-icon')), heading = rect(group.heading);
        const points = group.list.childElementCount === 1 ? [[heading.x, heading.bottom + 6], [icon.x, icon.top - 6]] :
          [[rect(group.section).left + 20, icon.top - 20], [icon.x, icon.top - 20], [icon.x, icon.top - 6]];
        d = this.roundedPath(points);
      }
      this.attr(this.edges.get(edge.id), 'd', d);
    }
  }

  async load() {
    if (this.pending || document.hidden) return;
    this.pending = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    try {
      const response = await fetch('/api/topology', { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error('Map request failed');
      const data = await response.json();
      this.validate(data);
      this.render(data);
    } catch (_) {
      this.failed = true;
      if (this.snapshot) this.tick();
      else {
        this.text(this.root.querySelector('#topology-summary'), 'Map unavailable');
        this.state(this.root.querySelector('#topology-summary'), 'error');
        this.text(this.root.querySelector('#topology-message'), 'We could not load your devices. Reconnecting automatically.');
        this.text(this.root.querySelector('#topology-updated'), 'Waiting for device information.');
        this.onStatus({ snapshot: null, stale: true });
      }
    } finally {
      clearTimeout(timeout);
      this.pending = false;
    }
  }

  start() {
    this.load();
    this.timer = setInterval(() => this.load(), this.refreshMs);
  }
}
