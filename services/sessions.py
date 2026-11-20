"""Penyimpan state per sesi.

Tidak ada state global yang dipakai bersama: mode, transkrip, hasil terakhir,
sampel jarak, dan ESP32 yang terpasang semuanya disimpan per ``session_id``.

* ESP32 yang bekerja mandiri memakai sesi otomatis ``dev-<device_id>``
  (``ensure_device_session``), tanpa HP/browser.
* Pairing lama (HP/browser <-> ESP32) tetap didukung lewat ``link_device``.
"""

import re
import threading
import time
import uuid


class SessionManager:
    def __init__(self, session_ttl_seconds=3600):
        self._sessions = {}       # session_id -> dict(state)
        self._device_link = {}    # esp32 device_id -> session_id
        self._web_sockets = {}    # session_id -> WSConnection wrapper
        self._esp32_sockets = {}  # device_id -> WSConnection wrapper
        self._esp32_ips = {}      # device_id -> IP lokal terakhir (untuk push audio PCM)
        self._lock = threading.Lock()
        self.session_ttl_seconds = session_ttl_seconds

    # ---------- session lifecycle ----------

    def create_session(self, session_id=None):
        session_id = session_id or str(uuid.uuid4())
        with self._lock:
            self._sessions.setdefault(session_id, {
                "created_at": time.time(),
                "last_seen": time.time(),
                "mode": None,
                "last_transcript": "",
                "last_result": "",
                "device_id": None,
            })
        return session_id

    def touch(self, session_id):
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id]["last_seen"] = time.time()

    def get(self, session_id):
        with self._lock:
            return dict(self._sessions[session_id]) if session_id in self._sessions else None

    def update(self, session_id, **kwargs):
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = {
                    "created_at": time.time(),
                    "mode": None,
                    "last_transcript": "",
                    "last_result": "",
                    "device_id": None,
                }
            self._sessions[session_id].update(kwargs)
            self._sessions[session_id]["last_seen"] = time.time()

    def cleanup_expired(self):
        now = time.time()
        expired = []
        with self._lock:
            for sid, s in list(self._sessions.items()):
                if now - s["last_seen"] > self.session_ttl_seconds:
                    expired.append(sid)
            for sid in expired:
                dev = self._sessions[sid].get("device_id")
                if dev and self._device_link.get(dev) == sid:
                    del self._device_link[dev]
                    self._esp32_sockets.pop(dev, None)
                self._sessions.pop(sid, None)
                self._web_sockets.pop(sid, None)
        return expired


    # ---------- request idempotency / duplicate-output guard ----------

    def begin_request(self, session_id, request_id, prompt_key=None, repeat_window_seconds=3.0):
        """Atomically claim one user request.

        Returns ``(status, cached_response)`` where status is one of:
        ``new``, ``cached``, or ``in_progress``.  The prompt-key guard also
        catches duplicate Web Speech ``onresult`` events that accidentally
        arrive with two different request IDs within a very short window.
        """
        now = time.time()
        request_id = str(request_id or "").strip()
        prompt_key = str(prompt_key or "").strip()
        with self._lock:
            state = self._sessions.setdefault(session_id, {
                "created_at": now,
                "last_seen": now,
                "mode": None,
                "last_transcript": "",
                "last_result": "",
                "device_id": None,
            })

            if request_id and state.get("last_request_id") == request_id and state.get("last_response"):
                state["last_seen"] = now
                return "cached", dict(state["last_response"])

            if state.get("inflight_request_id"):
                state["last_seen"] = now
                return "in_progress", None

            if prompt_key:
                if (
                    state.get("inflight_prompt_key") == prompt_key
                    and state.get("inflight_request_id")
                ):
                    state["last_seen"] = now
                    return "in_progress", None

                if (
                    state.get("last_prompt_key") == prompt_key
                    and state.get("last_response")
                    and now - float(state.get("last_prompt_at", 0.0) or 0.0) < float(repeat_window_seconds)
                ):
                    state["last_seen"] = now
                    return "cached", dict(state["last_response"])

            state["inflight_request_id"] = request_id or None
            state["inflight_prompt_key"] = prompt_key or None
            state["last_seen"] = now
            return "new", None

    def finish_request(self, session_id, request_id, prompt_key, response_payload):
        now = time.time()
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                return
            state["last_request_id"] = str(request_id or "")
            state["last_prompt_key"] = str(prompt_key or "")
            state["last_prompt_at"] = now
            cached = dict(response_payload or {})
            # Do not keep large base64 preview images in long-lived session
            # state just for duplicate suppression.
            cached.pop("image_url", None)
            state["last_response"] = cached
            state["inflight_request_id"] = None
            state["inflight_prompt_key"] = None
            state["last_seen"] = now

    def fail_request(self, session_id, request_id):
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                return
            if not request_id or state.get("inflight_request_id") == str(request_id):
                state["inflight_request_id"] = None
                state["inflight_prompt_key"] = None
                state["last_seen"] = time.time()

    # ---------- HP <-> ESP32 pairing ----------

    def link_device(self, device_id, session_id):
        """Pasangkan satu ESP32 (device_id unik, dikonfigurasi di firmware)
        ke satu session HP. Satu device_id hanya boleh terhubung ke satu
        session pada satu waktu, begitu juga sebaliknya."""
        with self._lock:
            old_sid = self._device_link.get(device_id)
            if old_sid and old_sid != session_id and old_sid in self._sessions:
                self._sessions[old_sid]["device_id"] = None
                self._sessions[old_sid].pop("distance_samples", None)
            old_device = self._sessions.get(session_id, {}).get("device_id")
            if old_device and old_device != device_id:
                self._device_link.pop(old_device, None)
            if session_id in self._sessions:
                self._sessions[session_id].pop("distance_samples", None)
            self._device_link[device_id] = session_id
            if session_id in self._sessions:
                self._sessions[session_id]["device_id"] = device_id

    def session_for_device(self, device_id):
        with self._lock:
            return self._device_link.get(device_id)

    def ensure_device_session(self, device_id):
        """Sesi bawaan untuk ESP32 yang bekerja mandiri (tanpa HP/browser).

        Bila perangkat sudah dipasangkan ke sesi lain (pairing lama lewat
        halaman web), pairing itu dipertahankan.
        """
        device_id = str(device_id or "unknown").strip() or "unknown"
        existing = self.session_for_device(device_id)
        if existing:
            return existing
        session_id = "dev-" + re.sub(r"[^A-Za-z0-9_-]", "_", device_id)[:70]
        self.create_session(session_id)
        self.link_device(device_id, session_id)
        return session_id

    def record_distance(self, session_id, distance_cm):
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None:
                now = time.time()
                values = [(t, d) for t, d in state.get("distance_samples", []) if now-t <= 4]
                state["distance_samples"] = (values + [(now, distance_cm)])[-5:]
                state["last_seen"] = now

    def device_for_session(self, session_id):
        with self._lock:
            s = self._sessions.get(session_id)
            return s.get("device_id") if s else None

    # ---------- live websocket registry (for server -> device push) ----------

    def register_web_socket(self, session_id, ws):
        with self._lock:
            self._web_sockets[session_id] = ws

    def unregister_web_socket(self, session_id):
        with self._lock:
            self._web_sockets.pop(session_id, None)

    def get_web_socket(self, session_id):
        with self._lock:
            return self._web_sockets.get(session_id)

    def register_esp32_socket(self, device_id, ws):
        with self._lock:
            self._esp32_sockets[device_id] = ws

    def unregister_esp32_socket(self, device_id):
        with self._lock:
            self._esp32_sockets.pop(device_id, None)

    def get_esp32_socket(self, device_id):
        with self._lock:
            return self._esp32_sockets.get(device_id)

    def connected_device_ids(self):
        with self._lock:
            return sorted(self._esp32_sockets)

    def latest_distance(self, max_age_seconds=4):
        now = time.time()
        with self._lock:
            samples = []
            for session_id, state in self._sessions.items():
                device_id = state.get("device_id")
                for sampled_at, distance_cm in state.get("distance_samples", []):
                    age = now - sampled_at
                    if age <= max_age_seconds:
                        samples.append((sampled_at, distance_cm, device_id, session_id))
            if not samples:
                return None
            sampled_at, distance_cm, device_id, session_id = max(samples)
            return {
                "distance_cm": round(float(distance_cm), 1),
                "age_ms": max(0, int((now - sampled_at) * 1000)),
                "device_id": device_id,
                "session_id": session_id,
            }

    def set_device_ip(self, device_id, ip):
        """Simpan IP lokal ESP32 (dikirim saat register lewat WebSocket),
        dipakai untuk push audio PCM langsung ke http://<ip>:8080/tts/play."""
        if not device_id or not ip:
            return
        with self._lock:
            self._esp32_ips[device_id] = ip

    def get_device_ip(self, device_id):
        with self._lock:
            return self._esp32_ips.get(device_id)
