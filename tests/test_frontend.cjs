// Browser-independent tests for the dashboard's device-backed controls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8');
const appCode = fs.readFileSync(path.join(root, 'web/app.js'), 'utf8');
for (const section of ['statusHeading', 'intro', 'hardware', 'process', 'demo', 'site-footer']) {
  assert.ok(html.includes(`id="${section}"`) || html.includes(`class="${section}"`), `missing page section: ${section}`);
}
assert.doesNotMatch(html, /<video\b|type=["']file["']/i, 'the page must not use a browser camera or video upload');
assert.doesNotMatch(appCode, /video\s*:\s*true|cameraSelect|captureSnapshotBlob/i, 'app must not capture browser camera frames');

const elements = new Map();
const quickRequests = [];
const mediaRequests = [];
const windowListeners = {};
const websocketInstances = [];
const eventSources = [];

function element(id) {
  if (elements.has(id)) return elements.get(id);
  const attributes = {};
  const el = {
    id, value: '', hidden: false, disabled: false, textContent: '', dataset: {}, listeners: {},
    classList: {
      values: new Set(),
      toggle(name, force) {
        if (force === undefined ? !this.values.has(name) : force) this.values.add(name);
        else this.values.delete(name);
      },
      remove(name) { this.values.delete(name); },
    },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute(name, value) { attributes[name] = String(value); },
    getAttribute(name) { return attributes[name] ?? null; },
    removeAttribute(name) { delete attributes[name]; },
    pause() {},
    play: async () => {},
    querySelectorAll: () => [],
  };
  elements.set(id, el);
  return el;
}

const quickButtons = ['objek', 'ekspresi', 'rambu'].map((mode) => {
  const button = element(mode);
  button.dataset.mode = mode;
  return button;
});

class FakeWebSocket {
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    this.sent = [];
    websocketInstances.push(this);
    setTimeout(() => this.onopen && this.onopen(), 1);
  }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
}

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    eventSources.push(this);
    setTimeout(() => this.onopen && this.onopen(), 1);
  }
  emit(message) { if (this.onmessage) this.onmessage({ data: JSON.stringify(message) }); }
  close() { this.closed = true; }
}

const healthPayload = {
  status: 'success',
  camera: { configured: true, reachable: true },
  esp32: {
    connected_devices: ['spider-main-01'],
    distance: { distance_cm: 63.2, age_ms: 850, device_id: 'spider-main-01' },
  },
};

const context = vm.createContext({
  console, setTimeout, clearTimeout, setInterval, clearInterval, Blob, FormData, AbortController,
  crypto: require('node:crypto').webcrypto,
  localStorage: { getItem: () => 'session-test', setItem() {} },
  document: {
    getElementById: element,
    querySelectorAll: (selector) => selector === '.quick-command' ? quickButtons : [],
  },
  navigator: { mediaDevices: { getUserMedia: async (constraints) => {
    mediaRequests.push(constraints);
    return { getAudioTracks: () => [], getTracks: () => [] };
  } } },
  window: {
    addEventListener(type, fn) { windowListeners[type] = fn; },
    speechSynthesis: { cancel() {}, speak() {} },
  },
  WebSocket: FakeWebSocket,
  EventSource: FakeEventSource,
  location: { protocol: 'http:', host: 'localhost' },
  fetch: async (url, options = {}) => {
    if (url === '/api/client_config') return { ok: true, json: async () => ({ camera_source: 'server' }) };
    if (url === '/api/health') return { ok: true, json: async () => healthPayload };
    if (url === '/api/esp32/status') return { ok: true, json: async () => ({
      status: 'success',
      connected_devices: ['spider-main-01'],
      distance: healthPayload.esp32.distance,
    }) };
    if (url === '/api/quick') {
      quickRequests.push(options.body);
      return { ok: true, json: async () => ({
        result: 'Ada kursi di depan.', mode: options.body.get('mode'), warnings: [], processing_ms: 42,
      }) };
    }
    throw new Error(`Unexpected request: ${url}`);
  },
});

vm.runInContext(appCode, context);

async function waitFor(test) {
  const deadline = Date.now() + 2000;
  while (!test()) {
    if (Date.now() > deadline) throw new Error('Timed out waiting for frontend condition');
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

(async () => {
  await waitFor(() => websocketInstances.length === 1 && eventSources.length === 1 &&
    element('systemStatusLabel').textContent === 'Connected');
  assert.equal(element('wsStatus').dataset.state, 'connected');
  assert.equal(element('mainStatus').dataset.state, 'connected');
  assert.equal(element('camStatus').dataset.state, 'connected');
  assert.equal(element('sensorDistance').textContent, '63.2');
  assert.equal(mediaRequests.length, 0, 'dashboard load must not ask for camera or microphone access');

  quickButtons[0].listeners.click();
  await waitFor(() => quickRequests.length === 1 && !quickButtons[0].disabled);
  assert.equal(quickRequests[0].get('mode'), 'objek');
  assert.equal(quickRequests[0].get('session_id'), 'session-test');
  assert.equal(quickRequests[0].get('image'), null, 'the backend captures from ESP32-CAM itself');
  assert.equal(mediaRequests.length, 0);
  assert.equal(element('resultText').textContent, 'Ada kursi di depan.');
  assert.match(element('quickStatus').textContent, /backend/i);

  element('navToggle').listeners.click();
  assert.equal(element('navToggle').getAttribute('aria-expanded'), 'true');
  assert.ok(element('primaryNav').classList.values.has('is-open'));
  windowListeners.pagehide();
  console.log('PASS dashboard: sections, no browser camera/video, real status rendering, server-side capture request, and responsive nav toggle.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
