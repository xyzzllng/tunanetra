// Physical ESP32 events are displayed by SSE and must not trigger duplicate AI requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const elements = new Map();
const quickCalls = [];
const streams = [];
const sockets = [];
const windowListeners = {};

function element(id) {
  if (elements.has(id)) return elements.get(id);
  const attributes = {};
  const el = {
    id, value: '', disabled: false, textContent: '', dataset: {}, listeners: {},
    classList: { toggle() {}, remove() {} },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    setAttribute(name, value) { attributes[name] = String(value); },
    getAttribute(name) { return attributes[name] ?? null; },
    removeAttribute(name) { delete attributes[name]; },
    pause() {}, play: async () => {},
  };
  elements.set(id, el);
  return el;
}

const buttons = ['objek', 'ekspresi', 'rambu'].map((mode) => {
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
    sockets.push(this);
    setTimeout(() => this.onopen && this.onopen(), 1);
  }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() { this.readyState = 3; if (this.onclose) this.onclose(); }
}

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    streams.push(this);
    setTimeout(() => this.onopen && this.onopen(), 1);
  }
  emit(data) { if (this.onmessage) this.onmessage({ data: JSON.stringify(data) }); }
  close() { this.closed = true; }
  fail() { if (this.onerror) this.onerror(); }
}

const context = vm.createContext({
  console, setTimeout, clearTimeout, setInterval, clearInterval, Blob, FormData, AbortController,
  crypto: require('node:crypto').webcrypto,
  localStorage: { getItem: () => 'session-test', setItem() {} },
  document: {
    getElementById: element,
    querySelectorAll: (selector) => selector === '.quick-command' ? buttons : [],
  },
  window: { addEventListener(type, fn) { windowListeners[type] = fn; }, speechSynthesis: { cancel() {}, speak() {} } },
  WebSocket: FakeWebSocket,
  EventSource: FakeEventSource,
  location: { protocol: 'http:', host: 'localhost' },
  fetch: async (url, options = {}) => {
    if (url === '/api/client_config') return { ok: true, json: async () => ({ camera_source: 'server' }) };
    if (url === '/api/health') return { ok: true, json: async () => ({
      status: 'success',
      camera: { configured: true, reachable: false },
      esp32: { connected_devices: ['main-01'], distance: null },
    }) };
    if (url === '/api/esp32/status') return { ok: true, json: async () => ({
      status: 'success', connected_devices: ['main-01'], distance: null,
    }) };
    if (url === '/api/quick') {
      quickCalls.push(options.body);
      return { ok: true, json: async () => ({ result: 'Hasil uji', processing_ms: 10, warnings: [] }) };
    }
    throw new Error(`Unexpected request: ${url}`);
  },
});

vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8'), context);

async function waitFor(test) {
  const deadline = Date.now() + 2000;
  while (!test()) {
    if (Date.now() > deadline) throw new Error('Timed out waiting for frontend condition');
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

(async () => {
  await waitFor(() => streams.length === 1 && sockets.length === 1 && sockets[0].onmessage);
  assert.ok(streams[0].url.includes('/api/esp32/stream'));
  assert.ok(sockets[0].sent.some((message) => message.type === 'register' && message.role === 'web'));
  assert.equal(element('mainStatusLabel').textContent, 'Connected');
  assert.equal(element('camStatusLabel').textContent, 'Disconnected');

  const source = streams[0];
  source.emit({ type: 'ready', mapping: [] });
  source.emit({ type: 'quick_started', mode: 'objek', button: 1, label: 'Deteksi Objek' });
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(quickCalls.length, 0, 'physical button notification must not run a second AI request');
  assert.match(element('quickStatus').textContent, /Tombol 1/);

  source.emit({ type: 'quick_rejected', mode: 'ekspresi', button: 2, reason: 'sedang sibuk' });
  assert.match(element('quickStatus').textContent, /tidak dijalankan/);
  source.emit({ type: 'quick_result', mode: 'objek', http_status: 200, result: 'Ada kursi di depan.', processing_ms: 850 });
  assert.equal(element('resultText').textContent, 'Ada kursi di depan.');
  assert.match(element('quickStatus').textContent, /speaker ESP32/);
  assert.equal(quickCalls.length, 0);

  element('deviceIdInput').value = 'main-01';
  element('pairBtn').listeners.click();
  assert.ok(sockets[0].sent.some((message) => message.type === 'pair_device' && message.device_id === 'main-01'));

  source.fail();
  assert.equal(source.closed, true);
  buttons[1].listeners.click();
  await waitFor(() => quickCalls.length === 1 && buttons.every((button) => !button.disabled));
  assert.equal(quickCalls[0].get('mode'), 'ekspresi');
  assert.equal(quickCalls[0].get('image'), null);
  windowListeners.pagehide();
  console.log('PASS ESP32 monitor: SSE events do not duplicate physical commands, pairing is preserved, and manual backend commands still work.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
