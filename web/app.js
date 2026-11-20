"use strict";

function getOrCreateSessionId() {
  let id = localStorage.getItem("spidersense_session_id");
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() :
      "sess-" + Date.now() + "-" + Math.random().toString(16).slice(2);
    localStorage.setItem("spidersense_session_id", id);
  }
  return id;
}

function newRequestId() {
  return crypto.randomUUID ? crypto.randomUUID() :
    "req-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

const SESSION_ID = getOrCreateSessionId();
const els = {
  wsStatus: document.getElementById("wsStatus"),
  wsStatusLabel: document.getElementById("wsStatusLabel"),
  mainStatus: document.getElementById("mainStatus"),
  mainStatusLabel: document.getElementById("mainStatusLabel"),
  mainDeviceLabel: document.getElementById("mainDeviceLabel"),
  camStatus: document.getElementById("camStatus"),
  camStatusLabel: document.getElementById("camStatusLabel"),
  systemStatus: document.getElementById("systemStatus"),
  systemStatusLabel: document.getElementById("systemStatusLabel"),
  systemDetail: document.getElementById("systemDetail"),
  statusUpdated: document.getElementById("statusUpdated"),
  quickStatus: document.getElementById("quickStatus"),
  resultText: document.getElementById("resultText"),
  resultWarnings: document.getElementById("resultWarnings"),
  resultAudio: document.getElementById("resultAudio"),
  replayBtn: document.getElementById("replayBtn"),
  esp32Log: document.getElementById("esp32Log"),
  deviceStatus: document.getElementById("deviceStatus"),
  deviceIdInput: document.getElementById("deviceIdInput"),
  pairBtn: document.getElementById("pairBtn"),
  talkBtn: document.getElementById("talkBtn"),
  talkStatus: document.getElementById("talkStatus"),
  sensorDistance: document.getElementById("sensorDistance"),
  sensorTimestamp: document.getElementById("sensorTimestamp"),
  virtualButton: document.getElementById("virtualButton"),
  virtualButtonStatus: document.getElementById("virtualButtonStatus"),
  navToggle: document.getElementById("navToggle"),
  primaryNav: document.getElementById("primaryNav"),
};

const quickButtons = Array.from(document.querySelectorAll(".quick-command"));
const ESP32_MODES = ["objek", "ekspresi", "rambu"];
let ws = null;
let wsReconnectTimer = null;
let wsRetryMs = 2000;
let esp32Source = null;
let esp32ReconnectTimer = null;
let esp32RetryMs = 2000;
let healthTimer = null;
let deviceTelemetryTimer = null;
let quickBusy = false;
let requestBusy = false;
let lastAudioUrl = null;
let audioGeneration = 0;
let disposed = false;
let mediaRecorder = null;
let audioStream = null;
let audioChunks = [];
let isRecording = false;
let stopRequested = false;
let recordingTimer = null;
let webSocketConnected = false;
let backendReachable = false;
let deviceTelemetryKnown = false;
let connectedDevices = [];
let cameraReachable = null;
let clientConfig = {};

function setText(element, text) {
  if (element) element.textContent = text;
}

function setIndicator(dot, label, state, text) {
  if (dot) dot.dataset.state = state;
  setText(label, text);
}

function updateSystemIndicator() {
  if (!backendReachable) {
    setIndicator(els.systemStatus, els.systemStatusLabel, "disconnected", "Disconnected");
    setText(els.systemDetail, "Backend belum dapat dijangkau");
    return;
  }
  if (webSocketConnected && connectedDevices.length > 0 && cameraReachable === true) {
    setIndicator(els.systemStatus, els.systemStatusLabel, "connected", "Connected");
    setText(els.systemDetail, "Backend dan perangkat utama siap");
    return;
  }
  setIndicator(els.systemStatus, els.systemStatusLabel, "standby", "Standby");
  if (!webSocketConnected) setText(els.systemDetail, "Menunggu kanal WebSocket");
  else if (!connectedDevices.length) setText(els.systemDetail, "Menunggu ESP32 Main");
  else if (cameraReachable === false) setText(els.systemDetail, "ESP32-CAM tidak terjangkau");
  else setText(els.systemDetail, "Menunggu status ESP32-CAM");
}

function updateDistance(distance) {
  if (!distance || typeof distance.distance_cm !== "number") {
    setText(els.sensorDistance, "—");
    setText(els.sensorTimestamp, "Menunggu data ultrasonik nyata");
    return;
  }
  setText(els.sensorDistance, distance.distance_cm.toFixed(1));
  const age = Number(distance.age_ms);
  const source = distance.device_id ? `ESP32 ${distance.device_id}` : "ESP32 Main";
  setText(els.sensorTimestamp, `${source} · diperbarui ${age < 1000 ? "baru saja" : `${Math.round(age / 1000)} dtk lalu`}`);
}

function updateDeviceStatus() {
  if (connectedDevices.length) {
    const names = connectedDevices.join(", ");
    setIndicator(els.mainStatus, els.mainStatusLabel, "connected", "Connected");
    setText(els.mainDeviceLabel, names);
    setText(els.deviceStatus, `ESP32 Main terhubung: ${names}`);
  } else {
    const state = backendReachable && deviceTelemetryKnown ? "disconnected" : "standby";
    setIndicator(els.mainStatus, els.mainStatusLabel, state,
      state === "disconnected" ? "Disconnected" : "Checking");
    setText(els.mainDeviceLabel, "Physical buttons & sensor");
    setText(els.deviceStatus, "ESP32 Main belum terdaftar pada backend.");
  }
}

function updateDeviceTelemetry(esp32) {
  connectedDevices = Array.isArray(esp32?.connected_devices) ? esp32.connected_devices : [];
  deviceTelemetryKnown = true;
  updateDeviceStatus();
  updateDistance(esp32?.distance);
  updateSystemIndicator();
}

async function refreshSystemStatus() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) throw new Error(`Health check gagal (${response.status})`);
    const health = await response.json();
    if (health.status !== "success") throw new Error("Health check tidak tersedia");
    backendReachable = true;
    updateDeviceTelemetry(health.esp32);
    cameraReachable = health.camera?.configured === false
      ? false
      : typeof health.camera?.reachable === "boolean" ? health.camera.reachable : null;
    setIndicator(els.camStatus, els.camStatusLabel,
      cameraReachable === true ? "connected" : cameraReachable === false ? "disconnected" : "standby",
      cameraReachable === true ? "Connected" : cameraReachable === false ? "Disconnected" : "Standby");
    updateDeviceStatus();
    updateDistance(health.esp32?.distance);
    updateSystemIndicator();
    const checkedAt = new Date().toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    setText(els.statusUpdated, `Updated ${checkedAt}`);
  } catch (error) {
    backendReachable = false;
    deviceTelemetryKnown = false;
    cameraReachable = null;
    connectedDevices = [];
    setIndicator(els.camStatus, els.camStatusLabel, "standby", "Standby");
    updateDeviceStatus();
    updateDistance(null);
    updateSystemIndicator();
    setText(els.statusUpdated, "Status server tidak tersedia");
    console.warn("Status Spider Sense tidak dapat diperbarui:", error);
  }
}

async function refreshDeviceTelemetry() {
  try {
    const response = await fetch("/api/esp32/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`Device status gagal (${response.status})`);
    const status = await response.json();
    if (status.status !== "success") throw new Error("Device status tidak tersedia");
    updateDeviceTelemetry(status);
  } catch (error) {
    deviceTelemetryKnown = false;
    connectedDevices = [];
    updateDeviceStatus();
    updateDistance(null);
    updateSystemIndicator();
  }
}

function setControlsBusy(busy) {
  quickButtons.forEach((button) => { button.disabled = busy; });
}

function speak(text) {
  if (!text || !window.speechSynthesis || typeof SpeechSynthesisUtterance === "undefined") return;
  try {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "id-ID";
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  } catch (error) {
    console.warn("Browser tidak dapat memutar teks:", error);
  }
}

function stopOutput() {
  audioGeneration += 1;
  if (els.resultAudio) {
    els.resultAudio.pause();
    els.resultAudio.removeAttribute("src");
  }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  lastAudioUrl = null;
  return audioGeneration;
}

function playResultAudio(url, fallbackText = "") {
  if (!url || !els.resultAudio) {
    if (fallbackText) speak(fallbackText);
    return;
  }
  lastAudioUrl = url;
  els.resultAudio.src = url;
  const playback = els.resultAudio.play();
  if (playback && typeof playback.catch === "function") {
    playback.catch(() => { if (fallbackText) speak(fallbackText); });
  }
}

els.replayBtn?.addEventListener("click", () => {
  if (!lastAudioUrl) {
    speak(els.resultText?.textContent || "");
    return;
  }
  els.resultAudio.src = lastAudioUrl;
  const playback = els.resultAudio.play();
  if (playback && typeof playback.catch === "function") {
    playback.catch(() => speak(els.resultText?.textContent || ""));
  }
});

async function pollQuickAudio(url, text, generation) {
  for (let attempt = 0; attempt < 60 && generation === audioGeneration && !disposed; attempt++) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 10000);
      let response;
      try { response = await fetch(url, { signal: controller.signal, cache: "no-store" }); }
      finally { clearTimeout(timer); }
      const data = await response.json();
      if (generation !== audioGeneration) return;
      if (data.status === "ready") {
        lastAudioUrl = data.audio_url;
        if (data.audio_output === "browser") playResultAudio(data.audio_url, text);
        else if (els.resultAudio) els.resultAudio.src = data.audio_url;
        return;
      }
      if (data.status === "superseded") return;
      if (data.status !== "pending") { speak(text); return; }
    } catch (error) {
      if (generation === audioGeneration) speak(text);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  if (generation === audioGeneration) setText(els.quickStatus, "Hasil siap. Audio masih diproses; gunakan tombol Dengar hasil.");
}

async function runQuick(mode) {
  if (!ESP32_MODES.includes(mode) || quickBusy || requestBusy || isRecording) return;
  quickBusy = true;
  const generation = stopOutput();
  setControlsBusy(true);
  setText(els.quickStatus, "Mengirim permintaan ke backend. Gambar diambil oleh ESP32-CAM bila tersedia…");
  setText(els.resultWarnings, "");
  setText(els.resultText, "Menunggu hasil deteksi…");

  try {
    const form = new FormData();
    form.append("session_id", SESSION_ID);
    form.append("request_id", newRequestId());
    form.append("mode", mode);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    let response;
    try {
      response = await fetch("/api/quick", { method: "POST", body: form, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || `Server error ${response.status}`);
    setText(els.resultText, data.result || "Deteksi selesai, tetapi backend tidak mengembalikan ringkasan.");
    setText(els.resultWarnings, (data.warnings || []).join("\n"));
    setText(els.quickStatus, `Selesai (${data.processing_ms ?? "—"} ms). Pemrosesan dilakukan di backend.`);
    if (data.audio_job) pollQuickAudio(data.audio_job, data.result, generation);
    else if (data.result) speak(data.result);
  } catch (error) {
    const message = error.name === "AbortError"
      ? "Backend terlalu lama merespons. Periksa status server dan perangkat."
      : error.message || "Permintaan deteksi gagal.";
    setText(els.resultText, message);
    setText(els.quickStatus, "Permintaan tidak berhasil. Periksa koneksi ESP32-CAM dan backend.");
  } finally {
    quickBusy = false;
    setControlsBusy(false);
  }
}

function setConnectionStatus(state, label) {
  webSocketConnected = state === "connected";
  setIndicator(els.wsStatus, els.wsStatusLabel, state, label);
  updateSystemIndicator();
}

function scheduleWebSocketReconnect() {
  if (disposed || wsReconnectTimer) return;
  wsReconnectTimer = setTimeout(() => {
    wsReconnectTimer = null;
    connectWebSocket();
  }, wsRetryMs);
  wsRetryMs = Math.min(wsRetryMs * 2, 30000);
}

function connectWebSocket() {
  if (disposed) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  setConnectionStatus("standby", "Connecting");
  try {
    ws = new WebSocket(`${protocol}//${location.host}/ws`);
  } catch (error) {
    setConnectionStatus("disconnected", "Disconnected");
    scheduleWebSocketReconnect();
    return;
  }
  ws.onopen = () => {
    wsRetryMs = 2000;
    setConnectionStatus("connected", "Connected");
    ws.send(JSON.stringify({ type: "register", role: "web", session_id: SESSION_ID }));
  };
  ws.onclose = () => {
    setConnectionStatus("disconnected", "Disconnected");
    scheduleWebSocketReconnect();
  };
  ws.onerror = () => setConnectionStatus("disconnected", "Disconnected");
  ws.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (error) { return; }
    if (message.type === "registered") {
      setConnectionStatus("connected", "Connected");
    } else if (message.type === "paired") {
      setText(els.deviceStatus, `ESP32 ${message.device_id || ""} berhasil dipasangkan.`);
      refreshSystemStatus();
    } else if (message.type === "start_recording") {
      startRecording();
    } else if (message.type === "stop_recording") {
      stopRecording();
    } else if (message.type === "toggle") {
      setText(els.deviceStatus, message.state === "on" ? "Sistem perangkat aktif." : "Sistem perangkat standby.");
    } else if (message.type === "status") {
      setText(els.resultText, message.message || "");
    }
  };
}

els.pairBtn?.addEventListener("click", () => {
  const deviceId = els.deviceIdInput?.value.trim();
  if (!deviceId) {
    setText(els.deviceStatus, "Masukkan ID perangkat terlebih dahulu.");
    return;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    setText(els.deviceStatus, "WebSocket belum tersambung; perangkat belum dipasangkan.");
    return;
  }
  ws.send(JSON.stringify({ type: "pair_device", device_id: deviceId }));
  setText(els.deviceStatus, "Permintaan pairing dikirim ke ESP32.");
});

function setEsp32Status(text) {
  if (!connectedDevices.length) setText(els.deviceStatus, text);
}

function appendEsp32Log(text) {
  if (!els.esp32Log) return;
  const stamp = new Date().toLocaleTimeString("id-ID");
  const lines = (els.esp32Log.textContent || "").split("\n").filter(Boolean);
  lines.unshift(`${stamp} — ${text}`);
  els.esp32Log.textContent = lines.slice(0, 5).join("\n");
}

function handleEsp32Event(message) {
  if (!message || !message.type) return;
  const label = message.label || message.mode || "";
  if (message.type === "quick_started") {
    setText(els.quickStatus, `Tombol ${message.button} → ${label}. Backend mengambil gambar dari ESP32-CAM…`);
    appendEsp32Log(`Tombol ${message.button} → ${label}`);
  } else if (message.type === "quick_rejected") {
    const reason = message.reason || "perintah tidak dimulai";
    setText(els.quickStatus, `Tombol ${message.button} tidak dijalankan: ${reason}`);
    appendEsp32Log(`Tombol ${message.button} ditolak: ${reason}`);
  } else if (message.type === "quick_result") {
    setText(els.resultText, message.result || "Backend tidak mengembalikan hasil.");
    setText(els.quickStatus, message.http_status === 200
      ? `Selesai (${message.processing_ms ?? "—"} ms). Audio diarahkan ke speaker ESP32.`
      : "Perintah tombol fisik gagal diproses.");
    if (message.http_status === 200) refreshSystemStatus();
  }
}

function scheduleEsp32Reconnect() {
  if (disposed || esp32ReconnectTimer) return;
  esp32ReconnectTimer = setTimeout(() => {
    esp32ReconnectTimer = null;
    connectEsp32Stream();
  }, esp32RetryMs);
  esp32RetryMs = Math.min(esp32RetryMs * 2, 30000);
}

function connectEsp32Stream() {
  if (disposed || typeof EventSource === "undefined") return;
  if (esp32Source) {
    esp32Source.close();
    esp32Source = null;
  }
  try {
    esp32Source = new EventSource(`/api/esp32/stream?session_id=${encodeURIComponent(SESSION_ID)}`);
  } catch (error) {
    scheduleEsp32Reconnect();
    return;
  }
  esp32Source.onopen = () => { esp32RetryMs = 2000; };
  esp32Source.onmessage = (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (error) { return; }
    if (message.type === "ready") return;
    handleEsp32Event(message);
  };
  esp32Source.onerror = () => {
    if (esp32Source) {
      esp32Source.close();
      esp32Source = null;
    }
    scheduleEsp32Reconnect();
  };
}

function ensureMicrophone() {
  if (audioStream && audioStream.getAudioTracks().some((track) => track.readyState === "live")) {
    return Promise.resolve(audioStream);
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
    return Promise.reject(new Error("Mikrofon tidak tersedia di browser ini."));
  }
  return navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    video: false,
  }).then((stream) => {
    audioStream = stream;
    return stream;
  });
}

function bestAudioMimeType() {
  if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
  return ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"]
    .find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function audioFileName(blob) {
  const type = (blob.type || "").toLowerCase();
  if (type.includes("ogg")) return "speech.ogg";
  if (type.includes("mp4")) return "speech.m4a";
  return "speech.webm";
}

async function startRecording() {
  if (isRecording || requestBusy || quickBusy) return;
  if (!window.MediaRecorder) {
    setText(els.talkStatus, "Browser ini tidak mendukung perekaman suara.");
    return;
  }
  isRecording = true;
  stopRequested = false;
  audioChunks = [];
  els.talkBtn?.setAttribute("aria-pressed", "true");
  setText(els.talkStatus, "Mendengarkan…");
  try {
    const stream = await ensureMicrophone();
    if (stopRequested) {
      isRecording = false;
      els.talkBtn?.setAttribute("aria-pressed", "false");
      return;
    }
    const mimeType = bestAudioMimeType();
    mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) audioChunks.push(event.data);
    };
    mediaRecorder.onerror = (event) => {
      console.error("MediaRecorder error", event.error || event);
      setText(els.talkStatus, "Perekaman suara gagal.");
    };
    mediaRecorder.onstop = async () => {
      if (recordingTimer) clearTimeout(recordingTimer);
      isRecording = false;
      els.talkBtn?.setAttribute("aria-pressed", "false");
      const blob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || audioChunks[0]?.type || "audio/webm" });
      audioChunks = [];
      if (blob.size < 256) {
        setText(els.talkStatus, "Rekaman terlalu singkat. Coba lagi.");
        return;
      }
      await sendToServer({ audioBlob: blob });
    };
    mediaRecorder.start(100);
    recordingTimer = setTimeout(stopRecording, 15000);
  } catch (error) {
    isRecording = false;
    els.talkBtn?.setAttribute("aria-pressed", "false");
    setText(els.talkStatus, `Mikrofon tidak dapat digunakan: ${error.message}`);
  }
}

function stopRecording() {
  stopRequested = true;
  if (recordingTimer) {
    clearTimeout(recordingTimer);
    recordingTimer = null;
  }
  if (mediaRecorder && mediaRecorder.state === "recording") mediaRecorder.stop();
  else if (isRecording) {
    isRecording = false;
    els.talkBtn?.setAttribute("aria-pressed", "false");
  }
}

els.talkBtn?.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  startRecording();
});
["pointerup", "pointercancel", "pointerleave"].forEach((type) => {
  els.talkBtn?.addEventListener(type, (event) => {
    event.preventDefault();
    stopRecording();
  });
});

async function sendToServer({ transcript = "", audioBlob = null } = {}) {
  if (requestBusy || quickBusy) return;
  requestBusy = true;
  stopOutput();
  setText(els.talkStatus, "Mengirim permintaan suara…");
  try {
    const form = new FormData();
    form.append("session_id", SESSION_ID);
    form.append("request_id", newRequestId());
    if (transcript) form.append("transcript", transcript);
    if (audioBlob) form.append("audio", audioBlob, audioFileName(audioBlob));
    const response = await fetch("/api/upload", { method: "POST", body: form });
    const data = await response.json().catch(() => ({}));
    if (data.status === "ignored" || data.duplicate_suppressed) {
      if (data.result) setText(els.resultText, data.result);
      setText(els.talkStatus, "Siap mendengarkan.");
      return;
    }
    if (!response.ok || !["success", "partial"].includes(data.status)) {
      throw new Error(data.message || "Layanan suara tidak tersedia.");
    }
    setText(els.resultText, data.result || "Tidak ada jawaban dari backend.");
    setText(els.quickStatus, "Permintaan suara selesai.");
    setText(els.talkStatus, "Siap mendengarkan.");
    if (data.audio_url) {
      const url = `${data.audio_url}?rid=${encodeURIComponent(data.request_id || "")}`;
      if (data.audio_output !== "esp32") playResultAudio(url, data.result);
      else lastAudioUrl = url;
    } else if (data.audio_output !== "esp32") {
      speak(data.result);
    }
  } catch (error) {
    setText(els.resultText, error.message || "Server tidak dapat terhubung.");
    setText(els.talkStatus, "Siap mendengarkan.");
  } finally {
    requestBusy = false;
    isRecording = false;
    els.talkBtn?.setAttribute("aria-pressed", "false");
  }
}

quickButtons.forEach((button) => button.addEventListener("click", () => runQuick(button.dataset.mode)));
els.virtualButton?.addEventListener("click", () => {
  els.virtualButton.setAttribute("aria-pressed", "true");
  setText(els.virtualButtonStatus, "Simulasi visual saja — perintah tidak dikirim ke ESP32.");
  setTimeout(() => els.virtualButton?.setAttribute("aria-pressed", "false"), 250);
});
els.navToggle?.addEventListener("click", () => {
  const expanded = els.navToggle.getAttribute("aria-expanded") === "true";
  els.navToggle.setAttribute("aria-expanded", String(!expanded));
  els.primaryNav?.classList.toggle("is-open", !expanded);
});
els.primaryNav?.querySelectorAll("a").forEach((link) => {
  link.addEventListener("click", () => {
    els.navToggle?.setAttribute("aria-expanded", "false");
    els.primaryNav?.classList.remove("is-open");
  });
});

window.addEventListener("pagehide", () => {
  disposed = true;
  stopOutput();
  if (healthTimer) clearInterval(healthTimer);
  if (deviceTelemetryTimer) clearInterval(deviceTelemetryTimer);
  if (wsReconnectTimer) clearTimeout(wsReconnectTimer);
  if (esp32ReconnectTimer) clearTimeout(esp32ReconnectTimer);
  if (ws) ws.close();
  if (esp32Source) esp32Source.close();
  if (audioStream) audioStream.getTracks().forEach((track) => track.stop());
});

async function start() {
  setIndicator(els.wsStatus, els.wsStatusLabel, "standby", "Connecting");
  setIndicator(els.mainStatus, els.mainStatusLabel, "standby", "Checking");
  setIndicator(els.camStatus, els.camStatusLabel, "standby", "Checking");
  try {
    const response = await fetch("/api/client_config", { cache: "no-store" });
    if (!response.ok) throw new Error(`Konfigurasi server gagal (${response.status})`);
    clientConfig = await response.json();
  } catch (error) {
    setText(els.quickStatus, "Konfigurasi backend tidak tersedia; periksa server.");
    console.warn("Konfigurasi Spider Sense tidak dapat dimuat:", error);
  }
  connectWebSocket();
  connectEsp32Stream();
  await refreshSystemStatus();
  await refreshDeviceTelemetry();
  healthTimer = setInterval(refreshSystemStatus, 30000);
  deviceTelemetryTimer = setInterval(refreshDeviceTelemetry, 2000);
}

window.runQuick = runQuick;
window.spiderSense = {
  MODES: ESP32_MODES,
  runQuick,
  deteksiObjek: () => runQuick("objek"),
  deteksiEkspresi: () => runQuick("ekspresi"),
  deteksiRambu: () => runQuick("rambu"),
  handleEsp32Event,
};

start();

/* ===== Router halaman berbasis hash: #home #components #workflow #simulation =====
   Setiap halaman hanya menampilkan satu .view; navbar & footer dipakai bersama.
   WebSocket/ESP32/kamera tetap berjalan di latar belakang saat berpindah halaman. */
const VIEW_IDS = ["home", "components", "workflow", "simulation"];
const VIEW_TITLES = {
  home: "Spider Sense — Smart Assistance System",
  components: "Components — Spider Sense",
  workflow: "Workflow — Spider Sense",
  simulation: "Simulation — Spider Sense",
};
function showView() {
  const hash = location.hash.slice(1);
  const id = VIEW_IDS.includes(hash) ? hash : "home";
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("is-active", view.id === `v-${id}`));
  document.querySelectorAll(".nav-links > a").forEach((link) => {
    const active = link.getAttribute("href") === `#${id}`;
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  document.title = VIEW_TITLES[id];
  window.scrollTo({ top: 0, behavior: "instant" });
}
window.addEventListener("hashchange", showView);
showView(); // tampilkan halaman sesuai URL saat pertama dibuka (default: Home)

document.getElementById("backToTop")?.addEventListener("click", () => {
  window.scrollTo({ top: 0, behavior: "smooth" });
});