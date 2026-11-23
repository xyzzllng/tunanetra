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

/* ===== Hardware Mode: koneksi backend/WebSocket/ESP32 =====
   Logika di bawah ini sama dengan versi sebelumnya. Bedanya, koneksi tidak
   lagi dimulai otomatis saat halaman dibuka, melainkan hanya ketika pengguna
   memilih Hardware Mode (start) dan dihentikan penuh saat kembali ke
   Standalone Simulation (stopHardware). hwGen mencegah balapan jika pengguna
   berpindah mode dengan cepat. */
let hardwareActive = false;
let hwGen = 0;

async function start() {
  if (hardwareActive) return;
  hardwareActive = true;
  disposed = false;
  wsRetryMs = 2000;
  esp32RetryMs = 2000;
  const gen = ++hwGen;
  const stale = () => !hardwareActive || gen !== hwGen;
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
  if (stale()) return;
  connectWebSocket();
  connectEsp32Stream();
  await refreshSystemStatus();
  if (stale()) return;
  await refreshDeviceTelemetry();
  if (stale()) return;
  healthTimer = setInterval(refreshSystemStatus, 30000);
  deviceTelemetryTimer = setInterval(refreshDeviceTelemetry, 2000);
}

function stopHardware() {
  if (!hardwareActive) return;
  hardwareActive = false;
  hwGen += 1;
  disposed = true; // menghentikan reconnect WebSocket/SSE dan polling audio
  if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  if (deviceTelemetryTimer) { clearInterval(deviceTelemetryTimer); deviceTelemetryTimer = null; }
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  if (esp32ReconnectTimer) { clearTimeout(esp32ReconnectTimer); esp32ReconnectTimer = null; }
  if (isRecording) stopRecording();
  if (ws) { ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null; try { ws.close(); } catch (e) { /* abaikan */ } ws = null; }
  if (esp32Source) { esp32Source.close(); esp32Source = null; }
  if (audioStream) { audioStream.getTracks().forEach((track) => track.stop()); audioStream = null; }
  stopOutput();
  webSocketConnected = false;
  backendReachable = false;
  deviceTelemetryKnown = false;
  connectedDevices = [];
  cameraReachable = null;
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

// start() tidak lagi dipanggil otomatis: Hardware Mode dimulai dari pemilih mode
// (lihat modul "Standalone Simulation Mode" di bagian paling bawah file ini).

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
/* =====================================================================
   STANDALONE SIMULATION MODE + pemilih mode
   ---------------------------------------------------------------------
   Seluruh kode di bawah berjalan 100% di browser: tidak memanggil /api/*,
   tidak membuka WebSocket/EventSource, tidak memakai ESP32 maupun kamera
   laptop. Semua hasil adalah DATA CONTOH yang sudah ditentukan; tidak ada
   model AI yang dijalankan dan gambar tidak dianalisis.
   Hardware Mode memakai kode lama di atas (start / stopHardware).
   ===================================================================== */
(function setupSimulation() {
  const $ = (id) => document.getElementById(id);
  const panelSim = $("simPanel");
  const panelHw = $("hardwarePanel");
  if (!panelSim || !panelHw) return;

  /* ---------- Data mode ---------- */
  const MODES = {
    objek: { key: 1, name: "Deteksi Objek" },
    ekspresi: { key: 2, name: "Deteksi Ekspresi" },
    rambu: { key: 3, name: "Deteksi Rambu" },
  };

  /* ---------- Gambar contoh (SVG dibuat di browser, tanpa file gambar) ---------- */
  const svgUri = (svg) => "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);

  function drawSign(x, type) {
    const cy = 130, base = 330;
    const pole = `<rect x="${x - 3}" y="${cy + 40}" width="6" height="${base - cy - 40}" fill="#6B6354"/>`;
    let shape;
    if (type === "stop") {
      const pts = Array.from({ length: 8 }, (_, k) => {
        const a = (Math.PI / 180) * (22.5 + 45 * k);
        return `${(x + 44 * Math.cos(a)).toFixed(1)},${(cy + 44 * Math.sin(a)).toFixed(1)}`;
      }).join(" ");
      shape = `<polygon points="${pts}" fill="#B4523A" stroke="#fff" stroke-width="3"/><text x="${x}" y="${cy + 8}" text-anchor="middle" font-family="Arial,sans-serif" font-weight="700" font-size="21" fill="#fff">STOP</text>`;
    } else if (type === "noentry") {
      shape = `<circle cx="${x}" cy="${cy}" r="42" fill="#B4523A" stroke="#fff" stroke-width="3"/><rect x="${x - 26}" y="${cy - 7}" width="52" height="14" rx="3" fill="#fff"/>`;
    } else {
      shape = `<rect x="${x - 42}" y="${cy - 42}" width="84" height="84" rx="10" fill="#4F6D8A" stroke="#fff" stroke-width="3"/><polygon points="${x},${cy - 30} ${x + 28},${cy + 22} ${x - 28},${cy + 22}" fill="#fff"/><circle cx="${x}" cy="${cy - 4}" r="4.5" fill="#4F6D8A"/><path d="M${x} ${cy + 1}v12m0 0-5 7m5-7 5 7M${x - 7} ${cy + 5}h14" stroke="#4F6D8A" stroke-width="3" stroke-linecap="round" fill="none"/>`;
    }
    return { svg: pole + shape, box: [x - 46, cy - 46, 92, 92] };
  }

  function drawPerson(x, mood) {
    const base = 360, cy = base - 205;
    const eyeR = mood === "surprised" ? 4.6 : 3.4;
    let mouth;
    if (mood === "happy") mouth = `<path d="M${x - 12} ${cy + 9}Q${x} ${cy + 23} ${x + 12} ${cy + 9}" stroke="#403B32" stroke-width="3" fill="none" stroke-linecap="round"/>`;
    else if (mood === "surprised") mouth = `<ellipse cx="${x}" cy="${cy + 15}" rx="6" ry="8" fill="#403B32"/>`;
    else mouth = `<path d="M${x - 9} ${cy + 14}h18" stroke="#403B32" stroke-width="3" stroke-linecap="round"/>`;
    const brows = mood === "surprised"
      ? `<path d="M${x - 17} ${cy - 13}q6-7 12 0M${x + 5} ${cy - 13}q6-7 12 0" stroke="#403B32" stroke-width="2.5" fill="none" stroke-linecap="round"/>` : "";
    const svg = `<rect x="${x - 24}" y="285" width="20" height="75" rx="5" fill="#403B32"/><rect x="${x + 4}" y="285" width="20" height="75" rx="5" fill="#403B32"/>
      <rect x="${x - 34}" y="187" width="68" height="105" rx="22" fill="#78866B"/>
      <circle cx="${x}" cy="${cy}" r="32" fill="#E5C3A6" stroke="#C9A58A" stroke-width="2"/>
      <path d="M${x - 32} ${cy - 4}A32 32 0 0 1 ${x + 32} ${cy - 4}Q${x} ${cy - 22} ${x - 32} ${cy - 4}Z" fill="#403B32"/>
      <circle cx="${x - 11}" cy="${cy - 3}" r="${eyeR}" fill="#403B32"/><circle cx="${x + 11}" cy="${cy - 3}" r="${eyeR}" fill="#403B32"/>${brows}${mouth}`;
    return { svg, box: [x - 36, cy - 36, 72, 72] };
  }

  function drawObject(x, type) {
    const base = 368;
    if (type === "chair") {
      return {
        svg: `<rect x="${x - 34}" y="${base - 38}" width="8" height="38" fill="#7A4228"/><rect x="${x + 26}" y="${base - 38}" width="8" height="38" fill="#7A4228"/>
          <rect x="${x - 36}" y="${base - 50}" width="72" height="12" rx="3" fill="#A5603F"/>
          <rect x="${x - 36}" y="${base - 112}" width="10" height="66" rx="3" fill="#A5603F"/><rect x="${x - 36}" y="${base - 112}" width="44" height="10" rx="3" fill="#A5603F"/>`,
        box: [x - 40, base - 116, 80, 116],
      };
    }
    if (type === "bin") {
      return {
        svg: `<polygon points="${x - 28},${base - 86} ${x + 28},${base - 86} ${x + 22},${base} ${x - 22},${base}" fill="#5C6A50"/><rect x="${x - 32}" y="${base - 96}" width="64" height="10" rx="3" fill="#4F5C44"/>
          <path d="M${x - 12} ${base - 70}v56M${x}  ${base - 70}v56M${x + 12} ${base - 70}v56" stroke="#78866B" stroke-width="3" stroke-linecap="round"/>`,
        box: [x - 34, base - 98, 68, 98],
      };
    }
    return {
      svg: `<rect x="${x - 42}" y="${base - 58}" width="84" height="58" rx="3" fill="#C9A06A" stroke="#A8804F" stroke-width="2"/><rect x="${x - 6}" y="${base - 58}" width="12" height="58" fill="#E5D7C3" opacity=".8"/>`,
      box: [x - 44, base - 60, 88, 60],
    };
  }

  function buildScene(def) {
    const o = drawObject(def.obj.x, def.obj.type);
    const f = drawPerson(def.face.x, def.face.mood);
    const s = drawSign(def.sign.x, def.sign.type);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 400" width="640" height="400">
      <rect width="640" height="400" fill="${def.sky}"/><circle cx="${def.sun}" cy="64" r="30" fill="#F1D8C8"/>
      <g fill="${def.far}" opacity=".75"><rect x="18" y="176" width="92" height="130"/><rect x="118" y="208" width="68" height="98"/><rect x="430" y="190" width="84" height="116"/><rect x="522" y="164" width="100" height="142"/></g>
      <rect y="300" width="640" height="100" fill="${def.ground}"/><rect y="300" width="640" height="6" fill="#A8B59A"/>
      <path d="M0 392H640" stroke="#A8957C" stroke-width="2" stroke-dasharray="26 18"/>
      ${s.svg}${f.svg}${o.svg}</svg>`;
    const pct = (b) => [(b[0] / 640) * 100, (b[1] / 400) * 100, (b[2] / 640) * 100, (b[3] / 400) * 100];
    return Object.assign({}, def, {
      src: svgUri(svg),
      boxes: { objek: pct(o.box), ekspresi: pct(f.box), rambu: pct(s.box) },
    });
  }

  const SCENES = [
    { id: "trotoar", name: "Trotoar pagi", sky: "#F1E4CC", ground: "#D8C8AD", far: "#C9B79C", sun: 90,
      obj: { x: 150, type: "chair", label: "Kursi", phrase: "kursi", conf: 91 },
      face: { x: 350, mood: "happy", label: "Senang", phrase: "senang", conf: 87 },
      sign: { x: 540, type: "stop", label: "Rambu Berhenti (STOP)", phrase: "berhenti", conf: 94 } },
    { id: "persimpangan", name: "Persimpangan", sky: "#EADFC8", ground: "#CDBB9E", far: "#A8B59A", sun: 540,
      sign: { x: 110, type: "noentry", label: "Rambu Dilarang Masuk", phrase: "dilarang masuk", conf: 89 },
      face: { x: 300, mood: "surprised", label: "Terkejut", phrase: "terkejut", conf: 82 },
      obj: { x: 500, type: "bin", label: "Tong sampah", phrase: "tong sampah", conf: 88 } },
    { id: "taman", name: "Taman kota", sky: "#EFE6D6", ground: "#CFC3A4", far: "#78866B", sun: 320,
      face: { x: 120, mood: "neutral", label: "Netral", phrase: "netral", conf: 85 },
      obj: { x: 330, type: "box", label: "Kardus", phrase: "kardus", conf: 86 },
      sign: { x: 530, type: "crossing", label: "Rambu Penyeberangan", phrase: "penyeberangan pejalan kaki", conf: 92 } },
  ].map(buildScene);

  /* Hasil contoh tetap untuk gambar unggahan (gambar tidak dianalisis). */
  const GENERIC = {
    objek: { label: "Objek (contoh)", phrase: "objek contoh", conf: 80, box: [28, 30, 44, 56] },
    ekspresi: { label: "Ekspresi senang (contoh)", phrase: "senang", conf: 80, box: [36, 14, 28, 42] },
    rambu: { label: "Rambu berhenti (contoh)", phrase: "berhenti", conf: 80, box: [62, 12, 26, 36] },
  };

  /* ---------- State ---------- */
  const S = {
    appMode: "sim",
    mode: null,
    source: null,      // { kind, src, name, scene? }
    snapshot: null,    // { src, name, kind, scene?, n, time }
    snapN: 0,
    distance: 120,
    result: null,
    autoSpeak: false,
    uploadUrl: null,
    active: true,
  };
  let timers = [];
  const later = (fn, ms) => timers.push(setTimeout(fn, ms));
  const cancelRun = () => { timers.forEach(clearTimeout); timers = []; };
  const clock = (d) => d.toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  const E = {
    pbtns: Array.from(document.querySelectorAll(".sim-pbtn")),
    tabs: Array.from(document.querySelectorAll(".sim-tab")),
    led: $("simLed"), pushStatus: $("simPushStatus"),
    range: $("simDistance"), distVal: $("simDistanceValue"), zone: $("simZone"), echo: $("simEcho"),
    svg: $("simSensorSvg"), obj: $("simObj"), rings: Array.from(document.querySelectorAll("#simRings path")),
    previewImg: $("simPreviewImg"), flash: $("simFlash"), thumbs: $("simThumbs"),
    snapBtn: $("simSnapBtn"), uploadBtn: $("simUploadBtn"), upload: $("simUpload"),
    snapLed: $("simSnapLed"), snapStatus: $("simSnapStatus"),
    stage: $("simDetectStage"), detectImg: $("simDetectImg"), detectEmpty: $("simDetectEmpty"),
    box: $("simBox"), boxLabel: $("simBoxLabel"),
    process: $("simProcess"), processText: $("simProcessText"),
    resLabel: $("simResultLabel"), resDesc: $("simResultDesc"),
    conf: $("simConf"), confVal: $("simConfValue"), confFill: $("simConfFill"),
    speechText: $("simSpeechText"), speakBtn: $("simSpeakBtn"), stopBtn: $("simStopBtn"),
    autoSpeak: $("simAutoSpeak"), audioLed: $("simAudioLed"), audioStatus: $("simAudioStatus"),
    modeBtns: Array.from(document.querySelectorAll("[data-app-mode]")),
    banner: $("modeBanner"), bannerTitle: $("modeBannerTitle"), bannerText: $("modeBannerText"),
  };
  const setLed = (el, state) => { if (el) el.dataset.state = state; };

  /* ---------- Hasil deteksi contoh ---------- */
  function buildResult(mode) {
    const snap = S.snapshot;
    const scene = snap && snap.scene;
    const d = S.distance;
    let label, phrase, conf, box, generic = false;
    if (scene) {
      const item = mode === "objek" ? scene.obj : mode === "ekspresi" ? scene.face : scene.sign;
      label = item.label; phrase = item.phrase; conf = item.conf; box = scene.boxes[mode];
    } else {
      const g = GENERIC[mode];
      label = g.label; phrase = g.phrase; conf = g.conf; box = g.box; generic = true;
    }
    let desc, speech;
    if (mode === "objek") {
      desc = `${label} terdeteksi di depan Anda, kira-kira ${d} cm (jarak dari slider simulasi).`;
      speech = `Objek terdeteksi di depan Anda: ${phrase}, sekitar ${d} sentimeter.`;
    } else if (mode === "ekspresi") {
      desc = `Wajah di depan Anda tampak ${phrase}.`;
      speech = `Terdeteksi ekspresi ${phrase}.`;
    } else {
      desc = `${label} terdeteksi di sisi jalan.`;
      speech = `Terdeteksi rambu ${phrase}.`;
    }
    if (generic) desc += " Posisi kotak dan hasil ini contoh tetap; gambar Anda tidak dianalisis.";
    return { mode, label, desc, speech, conf, box, generic };
  }

  /* ---------- Render ---------- */
  function renderResult() {
    const r = S.result;
    const has = !!r;
    E.resLabel.textContent = has ? r.label : "—";
    E.resDesc.textContent = has ? r.desc : "Belum ada hasil. Pilih mode deteksi untuk melihat contoh hasil.";
    E.conf.hidden = !has;
    if (has) {
      E.confVal.textContent = `${r.conf}% · contoh`;
      E.confFill.style.width = `${r.conf}%`;
      const [l, t, w, h] = r.box;
      E.box.style.cssText = `left:${l}%;top:${t}%;width:${w}%;height:${h}%`;
      E.box.classList.toggle("label-in", t < 14);
      E.box.classList.toggle("label-right", l + w > 62);
      E.boxLabel.textContent = `${r.label} · DEMO`;
    }
    E.box.hidden = !has;
    E.speechText.textContent = has ? `“${r.speech}”` : "Belum ada hasil untuk dibacakan. Jalankan deteksi terlebih dahulu.";
    if (hasSpeech) E.speakBtn.disabled = !has;
  }

  function renderSnapshot() {
    const snap = S.snapshot;
    E.detectImg.hidden = !snap;
    E.detectEmpty.hidden = !!snap;
    if (snap) {
      E.detectImg.src = snap.src;
      E.detectImg.alt = `Snapshot simulasi nomor ${snap.n}: ${snap.name}`;
    }
  }

  function setProcessing(on, text) {
    E.stage.classList.toggle("is-processing", on);
    E.process.hidden = !on;
    if (on && text) E.processText.textContent = text;
    E.tabs.concat(E.pbtns).forEach((b) => b.classList.toggle("is-busy", on));
  }

  function setActiveMode(mode) {
    S.mode = mode;
    E.pbtns.concat(E.tabs).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.simMode === mode)));
  }

  /* ---------- Sumber gambar & snapshot ---------- */
  function setSource(src) {
    cancelRun();
    stopSpeech(true);
    S.source = src;
    S.snapshot = null;
    S.result = null;
    E.previewImg.src = src.src;
    E.previewImg.alt = src.kind === "upload" ? `Gambar unggahan: ${src.name}` : `Gambar contoh: ${src.name}`;
    Array.from(E.thumbs.children).forEach((b, i) => b.setAttribute("aria-pressed", String(src.kind === "sample" && src.index === i)));
    setProcessing(false);
    renderSnapshot();
    renderResult();
    setLed(E.snapLed, "idle");
    E.snapStatus.textContent = `Sumber: ${src.name}. Tekan “Ambil snapshot” untuk mengambil gambar simulasi.`;
  }

  function takeSnapshot() {
    cancelRun();
    S.snapN += 1;
    S.snapshot = { src: S.source.src, name: S.source.name, kind: S.source.kind, scene: S.source.scene, n: S.snapN, time: new Date() };
    S.result = null;
    E.flash.classList.remove("fire");
    void E.flash.offsetWidth; // memulai ulang animasi
    E.flash.classList.add("fire");
    setLed(E.snapLed, "done");
    E.snapStatus.textContent = `✓ Snapshot #${S.snapN} berhasil diambil (simulasi) · ${clock(S.snapshot.time)} · ${S.snapshot.name}`;
    renderSnapshot();
    renderResult();
  }

  /* ---------- Menjalankan deteksi (simulasi) ---------- */
  function runDetection(mode, fromButton) {
    if (!MODES[mode]) return;
    stopSpeech(true);
    const needSnap = fromButton || !S.snapshot;
    if (needSnap) takeSnapshot(); else cancelRun();
    setActiveMode(mode);
    S.result = null;
    renderResult();
    setProcessing(true, needSnap ? "Mengambil snapshot simulasi…" : "Menyiapkan simulasi…");
    setLed(E.led, "busy");
    const name = MODES[mode].name;
    E.pushStatus.textContent = fromButton
      ? `Tombol ${MODES[mode].key} ditekan → mode ${name} (simulasi). Memproses…`
      : `Mode ${name} dipilih (simulasi). Memproses…`;
    later(() => { E.processText.textContent = `Menjalankan simulasi ${name.toLowerCase()}…`; }, 500);
    later(() => { E.processText.textContent = "Menyusun hasil contoh…"; }, 1100);
    later(() => {
      S.result = buildResult(mode);
      setProcessing(false);
      renderResult();
      setLed(E.led, "done");
      E.pushStatus.textContent = `Selesai: ${name}. Hasil ditandai SIMULATION RESULT (data contoh).`;
      if (S.autoSpeak) speak();
    }, 1700);
  }

  /* ---------- Sensor ultrasonik (simulasi) ---------- */
  const SENSOR_X0 = 90, SENSOR_SPAN = 270, SENSOR_FRONT = 76;
  const objFront = () => SENSOR_X0 + (S.distance / 400) * SENSOR_SPAN;
  let phase = 0, last = 0, raf = 0;

  function drawRings(ph) {
    const fx = objFront();
    const span = Math.max(fx - SENSOR_FRONT, 10);
    E.rings.forEach((p, i) => {
      const q = (ph + i / 3) % 1;
      const x = SENSOR_FRONT + q * span;
      const r = 8 + q * 26;
      p.setAttribute("d", `M${x.toFixed(1)} ${(75 - r).toFixed(1)}A${r.toFixed(1)} ${r.toFixed(1)} 0 0 1 ${x.toFixed(1)} ${(75 + r).toFixed(1)}`);
      p.setAttribute("stroke-opacity", ((1 - q) * 0.9).toFixed(2));
    });
  }
  function frame(ts) {
    raf = requestAnimationFrame(frame);
    const dt = last ? Math.min((ts - last) / 1000, 0.1) : 0;
    last = ts;
    phase = (phase + dt / (0.9 + (S.distance / 400) * 1.8)) % 1;
    drawRings(phase);
  }
  const reducedMotion = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  function syncAnimation() {
    const run = S.appMode === "sim" && location.hash === "#simulation" && !document.hidden && !reducedMotion();
    if (run && !raf) { last = 0; raf = requestAnimationFrame(frame); }
    else if (!run && raf) { cancelAnimationFrame(raf); raf = 0; }
    if (!run) drawRings(0.3);
  }

  function updateDistance(cm) {
    S.distance = cm;
    E.range.value = String(cm);
    E.range.setAttribute("aria-valuetext", `${cm} sentimeter`);
    E.distVal.textContent = String(cm);
    E.obj.style.transform = `translateX(${objFront().toFixed(1)}px)`;
    E.echo.textContent = String(Math.round(cm * 58));
    const zone = cm < 50 ? "near" : cm <= 150 ? "mid" : "far";
    E.zone.dataset.zone = zone;
    E.zone.textContent = zone === "near" ? "DEKAT" : zone === "mid" ? "SEDANG" : "JAUH";
    if (!raf) drawRings(0.3);
    if (S.result && S.result.mode === "objek") { S.result = buildResult("objek"); renderResult(); }
  }

  /* ---------- Audio (speechSynthesis, tanpa backend) ---------- */
  const hasSpeech = "speechSynthesis" in window && typeof SpeechSynthesisUtterance !== "undefined";
  let speechId = 0;
  function pickVoice() {
    if (!hasSpeech) return null;
    return speechSynthesis.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith("id")) || null;
  }
  function audioState(state, text) { setLed(E.audioLed, state); E.audioStatus.textContent = text; E.stopBtn.disabled = state !== "busy"; }
  function stopSpeech(silent) {
    if (!hasSpeech) return;
    const wasSpeaking = speechSynthesis.speaking || speechSynthesis.pending;
    speechId += 1;
    speechSynthesis.cancel();
    if (wasSpeaking && !silent) audioState("idle", "Suara dihentikan.");
    else if (wasSpeaking) audioState("idle", "Audio siap.");
  }
  function speak() {
    if (!hasSpeech || !S.result) return;
    const id = ++speechId;
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(S.result.speech);
    u.lang = "id-ID";
    const voice = pickVoice();
    if (voice) u.voice = voice;
    u.onstart = () => { if (id === speechId) audioState("busy", "Memutar audio…"); };
    u.onend = () => { if (id === speechId) audioState("done", "Selesai diputar."); };
    u.onerror = () => { if (id === speechId) audioState("idle", "Audio tidak dapat diputar atau dihentikan."); };
    audioState("busy", voice ? "Memutar audio…" : "Memutar audio (suara Indonesia tidak ditemukan; memakai suara default browser)…");
    speechSynthesis.speak(u);
  }

  /* ---------- Pemilih mode: Standalone Simulation / Hardware ---------- */
  const BANNER = {
    sim: ["SIMULATION MODE", "Semua angka, gambar, dan hasil di bawah ini adalah data simulasi lokal di browser. Tidak ada permintaan ke backend, WebSocket, ESP32, atau ESP32-CAM."],
    hardware: ["HARDWARE MODE", "Menggunakan backend, WebSocket, ESP32 Main, dan ESP32-CAM yang sebenarnya. Status koneksi di bawah mencerminkan perangkat nyata; tidak ada data simulasi di panel ini."],
  };
  function setAppMode(mode) {
    S.appMode = mode;
    panelSim.hidden = mode !== "sim";
    panelHw.hidden = mode !== "hardware";
    E.modeBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.appMode === mode)));
    E.banner.dataset.mode = mode;
    E.bannerTitle.textContent = BANNER[mode][0];
    E.bannerText.textContent = BANNER[mode][1];
    if (mode === "hardware") {
      cancelRun();
      setProcessing(false);
      stopSpeech(true);
      syncAnimation();
      start();
    } else {
      stopHardware();
      syncAnimation();
    }
  }

  /* ---------- Event ---------- */
  E.modeBtns.forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.appMode !== S.appMode) setAppMode(b.dataset.appMode);
  }));
  E.pbtns.forEach((b) => b.addEventListener("click", () => runDetection(b.dataset.simMode, true)));
  E.tabs.forEach((b) => b.addEventListener("click", () => runDetection(b.dataset.simMode, false)));
  E.range.addEventListener("input", () => updateDistance(Number(E.range.value)));
  document.querySelectorAll(".sim-presets [data-cm]").forEach((b) => b.addEventListener("click", () => updateDistance(Number(b.dataset.cm))));
  E.snapBtn.addEventListener("click", () => { setProcessing(false); takeSnapshot(); });
  E.uploadBtn.addEventListener("click", () => E.upload.click());
  E.upload.addEventListener("change", () => {
    const file = E.upload.files && E.upload.files[0];
    E.upload.value = "";
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setLed(E.snapLed, "idle");
      E.snapStatus.textContent = "File bukan gambar. Pilih berkas JPG, PNG, atau WebP.";
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setLed(E.snapLed, "idle");
      E.snapStatus.textContent = "Gambar lebih dari 10 MB. Pilih gambar yang lebih kecil.";
      return;
    }
    if (S.uploadUrl) URL.revokeObjectURL(S.uploadUrl);
    S.uploadUrl = URL.createObjectURL(file);
    setSource({ kind: "upload", src: S.uploadUrl, name: file.name });
  });
  E.previewImg.addEventListener("error", () => {
    setLed(E.snapLed, "idle");
    E.snapStatus.textContent = "Browser tidak dapat menampilkan gambar ini. Coba format JPG, PNG, atau WebP.";
  });
  E.autoSpeak.addEventListener("change", () => { S.autoSpeak = E.autoSpeak.checked; });
  E.speakBtn.addEventListener("click", speak);
  E.stopBtn.addEventListener("click", () => stopSpeech(false));
  document.addEventListener("visibilitychange", syncAnimation);
  window.addEventListener("hashchange", () => {
    if (location.hash !== "#simulation") stopSpeech(true);
    syncAnimation();
  });
  window.addEventListener("pagehide", () => { cancelRun(); stopSpeech(true); if (S.uploadUrl) URL.revokeObjectURL(S.uploadUrl); });

  /* ---------- Inisialisasi ---------- */
  SCENES.forEach((scene, index) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sim-thumb";
    btn.setAttribute("aria-pressed", "false");
    btn.setAttribute("aria-label", `Pilih gambar contoh: ${scene.name}`);
    const img = document.createElement("img");
    img.src = scene.src; img.alt = ""; img.loading = "lazy";
    const cap = document.createElement("span");
    cap.textContent = scene.name;
    btn.append(img, cap);
    btn.addEventListener("click", () => setSource({ kind: "sample", index, src: scene.src, name: scene.name, scene }));
    E.thumbs.appendChild(btn);
  });
  if (!hasSpeech) {
    E.speakBtn.disabled = true;
    E.stopBtn.disabled = true;
    E.autoSpeak.disabled = true;
    audioState("idle", "Browser ini tidak mendukung Web Speech API (speechSynthesis). Hasil tetap tampil sebagai teks.");
    E.stopBtn.disabled = true;
  }
  setSource({ kind: "sample", index: 0, src: SCENES[0].src, name: SCENES[0].name, scene: SCENES[0] });
  E.snapStatus.textContent = "Belum ada snapshot. Tekan “Ambil snapshot” atau tombol virtual.";
  updateDistance(S.distance);

  // Standalone Simulation adalah mode default. ?mode=hardware membuka Hardware Mode langsung.
  const wantHardware = new URLSearchParams(location.search).get("mode") === "hardware";
  setAppMode(wantHardware ? "hardware" : "sim");
  window.spiderSenseSim = { setMode: setAppMode, runDetection, getState: () => ({ mode: S.appMode, distance: S.distance }) };
})();