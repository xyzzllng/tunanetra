"""Hub event tombol fisik ESP32 -> deteksi langsung di backend.

Alur (tanpa browser, tanpa webcam)::

    Tombol ESP32 --WebSocket /ws | POST /api/esp32/event | serial USB-->
        ESP32EventHub.dispatch()  (normalisasi, debounce, sesi perangkat)
            --> trigger()  = QuickCommandService.start_from_button()
                    --> ESP32-CAM /capture --> AI --> TTS --> tts_ready --> speaker ESP32

Hub tidak menjalankan deteksi sendiri; ia hanya router event. Monitor opsional
(halaman ``web/``) dapat mendengarkan ``GET /api/esp32/stream`` (SSE) untuk
melihat ``quick_started`` / ``quick_result``; monitor tidak memicu apa pun.

Semua jalur transport bermuara ke :meth:`ESP32EventHub.dispatch`, jadi
perilakunya identik.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque

from flask import Blueprint, Response, jsonify, request

# Nama event di "kabel" (sesuai permintaan integrasi) -> mode internal yang
# sudah dipakai /api/quick dan atribut data-mode pada tombol UI.
EVENT_TO_MODE = {
    "object_detection": "objek",
    "expression_detection": "ekspresi",
    "sign_detection": "rambu",
}

# Nomor tombol fisik ESP32 -> nama event.
BUTTON_TO_EVENT = {
    1: "object_detection",
    2: "expression_detection",
    3: "sign_detection",
}

# Label yang dibacakan/ditampilkan, mengikuti teks tombol di index.html.
EVENT_LABEL = {
    "object_detection": "Deteksi Objek",
    "expression_detection": "Deteksi Ekspresi",
    "sign_detection": "Deteksi Rambu Lalu Lintas",
}

# Penerimaan longgar supaya firmware/klien lama tidak perlu diubah.
EVENT_ALIASES = {
    "objek": "object_detection",
    "object": "object_detection",
    "deteksi_objek": "object_detection",
    "ekspresi": "expression_detection",
    "expression": "expression_detection",
    "wajah": "expression_detection",
    "deteksi_ekspresi": "expression_detection",
    "rambu": "sign_detection",
    "sign": "sign_detection",
    "traffic_sign": "sign_detection",
    "deteksi_rambu": "sign_detection",
    # Nama lama (firmware/klien lama): tetap diterima, tapi sekarang menjalankan
    # deteksi rambu. Tidak ada lagi deteksi teks.
    "text_detection": "sign_detection",
    "baca": "sign_detection",
}


def normalize_event(event=None, button=None):
    """Kembalikan ``(event_name, mode)`` dari nama event atau nomor tombol.

    Melempar ``ValueError`` bila tidak dikenali, supaya endpoint bisa membalas
    400 dengan pesan yang jelas alih-alih diam-diam mengabaikan input.
    """
    if event is not None and str(event).strip() != "":
        key = str(event).strip().lower().replace("-", "_").replace(" ", "_")
        key = EVENT_ALIASES.get(key, key)
        if key in EVENT_TO_MODE:
            return key, EVENT_TO_MODE[key]
        raise ValueError(f"Event '{event}' tidak dikenali")

    if button is not None and str(button).strip() != "":
        try:
            number = int(str(button).strip())
        except (TypeError, ValueError):
            raise ValueError(f"Nomor tombol '{button}' tidak valid") from None
        if number in BUTTON_TO_EVENT:
            name = BUTTON_TO_EVENT[number]
            return name, EVENT_TO_MODE[name]
        raise ValueError(f"Tombol {number} tidak dipetakan; gunakan 1, 2, atau 3")

    raise ValueError("Sertakan 'event' atau 'button'")


def event_button_number(event_name):
    for number, name in BUTTON_TO_EVENT.items():
        if name == event_name:
            return number
    return None


class _Subscriber:
    """Satu koneksi SSE milik satu tab browser."""

    __slots__ = ("session_id", "queue", "created_at")

    def __init__(self, session_id):
        self.session_id = session_id
        self.queue = queue.Queue(maxsize=32)
        self.created_at = time.time()


class ESP32EventHub:
    """Router event tombol fisik menuju layanan deteksi.

    Aman dipanggil dari thread mana pun (websocket handler, thread serial,
    request HTTP). Tidak pernah memblokir pemanggil: deteksi berjalan di
    thread pekerja, dan antrean monitor dibatasi (pesan tertua dibuang).
    """

    def __init__(self, sessions, *, cooldown_ms=700, shared_token="", max_clients=8,
                 heartbeat_seconds=15, logger=None, trigger=None):
        self._sessions = sessions
        self._lock = threading.Lock()
        self._subscribers = []          # list[_Subscriber]
        self._last_event_at = {}        # (device_id, event_name) -> monotonic
        self._log = deque(maxlen=60)
        self._counter = 0
        self.cooldown_ms = max(0, int(cooldown_ms))
        self.trigger = trigger          # callable(mode=, device_id=, session_id=, label=, source=) -> dict
        self.shared_token = str(shared_token or "").strip()
        self.max_clients = max(1, int(max_clients))
        self.heartbeat_seconds = max(5, int(heartbeat_seconds))
        self.logger = logger
        self.serial_bridge = None       # diisi oleh devices.serial_bridge bila aktif

    # ---------------------------------------------------------------- logging
    def _log_line(self, level, message, **extra):
        entry = {"at": time.time(), "level": level, "message": message}
        entry.update(extra)
        with self._lock:
            self._log.append(entry)
        if self.logger:
            getattr(self.logger, "info" if level == "info" else "warning")(f"[ESP32] {message}")

    def recent_log(self, limit=20):
        with self._lock:
            return list(self._log)[-int(limit):]

    # ------------------------------------------------------------ subscribers
    def subscribe(self, session_id):
        subscriber = _Subscriber(session_id)
        with self._lock:
            if len(self._subscribers) >= self.max_clients:
                # Buang koneksi tertua supaya reload halaman berulang tidak
                # menghabiskan thread server.
                oldest = self._subscribers.pop(0)
                try:
                    oldest.queue.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers.append(subscriber)
            count = len(self._subscribers)
        self._log_line("info", f"Browser terhubung ke stream (session={session_id}), total {count}")
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
            count = len(self._subscribers)
        self._log_line("info", f"Browser keluar dari stream, total {count}")

    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)

    def publish(self, payload):
        """Kirim informasi ke semua monitor SSE. Mengembalikan jumlah penerima."""
        with self._lock:
            subscribers = list(self._subscribers)
        return sum(1 for target in subscribers if self._push(target, payload))

    def _push(self, subscriber, payload):
        try:
            subscriber.queue.put_nowait(payload)
            return True
        except queue.Full:
            try:                       # buang yang tertua, simpan yang terbaru
                subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(payload)
                return True
            except (queue.Empty, queue.Full):
                return False

    # --------------------------------------------------------------- dispatch
    def dispatch(self, *, event=None, button=None, device_id=None, source="http"):
        """Terjemahkan satu penekanan tombol fisik menjadi event UI.

        Mengembalikan dict ringkasan.  ``ValueError`` untuk input tidak valid.
        """
        event_name, mode = normalize_event(event, button)
        device_id = (str(device_id).strip() if device_id else "") or "unknown"
        number = event_button_number(event_name)
        label = EVENT_LABEL[event_name]

        # Debounce lapis kedua di server. Debounce utama ada di firmware; ini
        # melindungi dari pantulan yang lolos, paket ganda, atau dua transport
        # (WebSocket + Serial) yang aktif bersamaan pada alat yang sama.
        now = time.monotonic()
        key = (device_id, event_name)
        with self._lock:
            previous = self._last_event_at.get(key)          # None: belum pernah ada event
            duplicate = previous is not None and (now - previous) * 1000.0 < self.cooldown_ms
            if not duplicate:
                self._last_event_at[key] = now
                self._counter += 1
                sequence = self._counter
            else:
                sequence = self._counter
        if duplicate:
            self._log_line("warn", f"Tombol {number} ({label}) diabaikan: terlalu cepat berulang",
                           device_id=device_id, event=event_name, source=source)
            return {"status": "ignored", "reason": "cooldown", "event": event_name,
                    "mode": mode, "button": number, "delivered": 0}

        # ESP32 tanpa halaman web tetap punya sesi: dibuat otomatis per device_id.
        session_id = self._sessions.ensure_device_session(device_id)

        outcome = {"status": "no_trigger", "reason": "layanan deteksi belum terpasang"}
        if self.trigger is not None:
            try:
                outcome = self.trigger(mode=mode, device_id=device_id, session_id=session_id,
                                       label=label, source=source) or outcome
            except Exception as exc:                        # pragma: no cover
                outcome = {"status": "error", "reason": str(exc)}
        started = outcome.get("status") == "started"

        payload = {"type": "quick_started" if started else "quick_rejected", "event": event_name,
                   "mode": mode, "button": number, "label": label, "device_id": device_id,
                   "session_id": session_id, "source": source, "sequence": sequence,
                   "reason": outcome.get("reason"), "at": time.time()}
        monitors = self.publish(payload)

        if started:
            self._log_line("info", f"Tombol {number} -> {label} dimulai (via {source})",
                           device_id=device_id, event=event_name, mode=mode, source=source)
        else:
            self._log_line("warn", f"Tombol {number} -> {label} tidak dimulai: {outcome.get('status')} "
                                   f"{outcome.get('reason', '')}".strip(),
                           device_id=device_id, event=event_name, mode=mode, source=source)

        # Umpan balik ke ESP32 lewat serial, kalau bridge serial dipakai.
        bridge = self.serial_bridge
        if bridge is not None and source == "serial":
            try:
                bridge.beep(900 if started else 400, 90)
            except Exception:                              # pragma: no cover
                pass

        return {"status": outcome.get("status"), "event": event_name, "mode": mode, "button": number,
                "label": label, "session_id": session_id, "delivered": 1 if started else 0,
                "monitors": monitors, "request_id": outcome.get("request_id")}

    # ----------------------------------------------------------------- status
    def status(self):
        bridge = self.serial_bridge
        return {
            "monitors": self.subscriber_count(),
            "trigger_ready": self.trigger is not None,
            "cooldown_ms": self.cooldown_ms,
            "auth_required": bool(self.shared_token),
            "connected_devices": self._sessions.connected_device_ids(),
            "distance": self._sessions.latest_distance(),
            "serial": bridge.status() if bridge is not None else {"enabled": False},
            "mapping": [
                {"button": number, "event": name, "mode": EVENT_TO_MODE[name], "label": EVENT_LABEL[name]}
                for number, name in sorted(BUTTON_TO_EVENT.items())
            ],
            "log": self.recent_log(20),
        }

    # ------------------------------------------------------------------- auth
    def token_ok(self, provided):
        if not self.shared_token:
            return True
        return str(provided or "").strip() == self.shared_token


def register_esp32_events(app, sessions, settings=None, logger=None):
    """Pasang endpoint ESP32 pada aplikasi Flask dan kembalikan hub-nya."""
    if settings is None:                                   # import lokal: hindari siklus
        from config.settings import settings as settings

    hub = ESP32EventHub(
        sessions,
        cooldown_ms=settings.esp32_event_cooldown_ms,
        shared_token=settings.esp32_shared_token,
        max_clients=settings.esp32_max_stream_clients,
        heartbeat_seconds=settings.esp32_sse_heartbeat_seconds,
        logger=logger or app.logger,
    )
    api = Blueprint("esp32_events", __name__)

    def _token_from_request():
        return (request.headers.get("X-ESP32-Token")
                or request.args.get("token")
                or (request.get_json(silent=True) or {}).get("token")
                or request.form.get("token"))

    @api.get("/api/esp32/stream")
    def stream():
        """Aliran informasi untuk monitor opsional. Satu koneksi, tanpa polling."""
        session_id = (request.args.get("session_id") or "").strip()
        subscriber = hub.subscribe(session_id)

        def generate():
            # Beri tahu monitor bahwa stream siap, termasuk peta tombol.
            yield "retry: 3000\n\n"
            hello = {"type": "ready", "session_id": session_id,
                     "mapping": hub.status()["mapping"]}
            yield f"data: {json.dumps(hello)}\n\n"
            try:
                while True:
                    try:
                        payload = subscriber.queue.get(timeout=hub.heartbeat_seconds)
                    except queue.Empty:
                        # Komentar SSE: menjaga koneksi hidup melewati proxy,
                        # tidak memicu onmessage di browser.
                        yield ": keepalive\n\n"
                        continue
                    if payload is None:
                        break
                    yield f"data: {json.dumps(payload)}\n\n"
            finally:
                hub.unsubscribe(subscriber)

        return Response(generate(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        })

    @api.post("/api/esp32/event")
    def receive_event():
        """Endpoint HTTP untuk ESP32, serial bridge, dan pengujian manual."""
        if not hub.token_ok(_token_from_request()):
            return jsonify(status="error", message="Token ESP32 tidak cocok"), 401
        data = request.get_json(silent=True) or request.form or {}
        try:
            result = hub.dispatch(
                event=data.get("event"),
                button=data.get("button"),
                device_id=data.get("device_id"),
                source=str(data.get("source") or "http"),
            )
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        code = 200 if result["status"] in {"started", "busy", "ignored"} else 503
        return jsonify(result), code

    @api.get("/api/esp32/status")
    def esp32_status():
        return jsonify(status="success", **hub.status())

    app.register_blueprint(api)
    app.extensions["esp32_hub"] = hub
    return hub
