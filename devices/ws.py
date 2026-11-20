"""WebSocket ``/ws`` untuk ESP32 (dan monitor web opsional).

ESP32 mendaftar dengan ``{"type":"register","role":"esp32","device_id":...}``.
Pesan yang dipahami dari ESP32:

* ``quick_command``  - tombol 1/2/3 (``event`` atau ``button``) -> hub -> deteksi
                        langsung di backend; dibalas ``quick_ack``.
* ``quick_command`` dengan ``event="obstacle_warning"`` (firmware esp32_main) atau
  ``ultrasonic`` (firmware esp32_multidevice) - sampel jarak sensor.
* ``button_down`` / ``button_up`` / ``toggle`` - push-to-talk & saklar (hanya
  berguna bila ada klien yang merekam suara, mis. halaman web opsional).

Pesan ke ESP32: ``registered``, ``quick_ack``, ``tts_ready`` (URL MP3), ``error``,
``paired``.
"""
from __future__ import annotations

import json
import math
import re
import threading

from flask_sock import Sock
from simple_websocket import ConnectionClosed

from services.esp32_audio_push import push_mp3_to_esp32

QUICK_TYPES = ("quick_command", "quick", "button_press")


class SafeWS:
    """Pembungkus ws agar aman dikirimi pesan dari thread lain."""

    def __init__(self, ws, logger=None):
        self.ws = ws
        self.lock = threading.Lock()
        self.log = logger

    def send_json(self, payload):
        data = json.dumps(payload)
        with self.lock:
            try:
                self.ws.send(data)
                return True
            except Exception as exc:
                if self.log:
                    self.log.warning("Gagal kirim ke websocket: %s", exc)
                return False


def valid_distance(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 2 <= value <= 500)


def ultrasonic_message(distance_cm):
    """>150 cm diam, 100-150 info ringan, 50-100 peringatan; <50 cm ditangani lokal oleh firmware."""
    if distance_cm > 150:
        return None
    if distance_cm > 100:
        return f"Ada objek sekitar {int(distance_cm)} sentimeter di depan."
    if distance_cm > 50:
        return f"Perhatian, ada halangan sekitar {int(distance_cm)} sentimeter."
    return None


def safe_token(value, fallback="x"):
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return token[:80] or fallback


class DeviceLink:
    """Registri socket + endpoint WebSocket. Satu instance per aplikasi."""

    def __init__(self, app, sessions, hub, audio_getter, audio_dir):
        self.app = app
        self.sessions = sessions
        self.hub = hub
        self.audio_getter = audio_getter
        self.audio_dir = audio_dir
        self.log = app.logger
        self.sock = Sock(app)
        self.sock.route("/ws")(self._endpoint)
        app.extensions["device_link"] = self

    # ------------------------------------------------------------- push
    def push_to_device(self, device_id, payload):
        ws = self.sessions.get_esp32_socket(device_id)
        return bool(ws and ws.send_json(payload))

    def push_to_session(self, session_id, payload):
        ws = self.sessions.get_web_socket(session_id)
        if ws:
            ws.send_json(payload)

    def _save_tts(self, text, filename):
        audio = self.audio_getter()
        if not audio:
            return None
        try:
            path = self.audio_dir / filename
            path.write_bytes(audio.generate_tts(text).getbuffer())
            return path
        except Exception as exc:
            self.log.warning("TTS gagal: %s", exc)
            return None

    # --------------------------------------------------------- endpoint
    def _endpoint(self, ws):
        safe_ws = SafeWS(ws, self.log)
        sessions = self.sessions
        role = session_id = device_id = None
        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")

                if mtype == "register":
                    role = msg.get("role")
                    if role == "web":
                        session_id = msg.get("session_id") or sessions.create_session()
                        sessions.create_session(session_id)
                        sessions.register_web_socket(session_id, safe_ws)
                        safe_ws.send_json({"type": "registered", "session_id": session_id})
                    elif role == "esp32":
                        device_id = msg.get("device_id")
                        if not device_id:
                            safe_ws.send_json({"type": "error", "message": "device_id wajib diisi"})
                            continue
                        sessions.register_esp32_socket(device_id, safe_ws)
                        # Firmware baru butuh IP ini untuk push audio PCM langsung
                        # (POST /tts/play), bukan lewat jalur pull tts_ready lama.
                        ip = msg.get("ip")
                        if ip:
                            sessions.set_device_ip(device_id, ip)
                            self.log.info("ESP32 %s IP tercatat: %s", device_id, ip)
                        else:
                            self.log.warning(
                                "ESP32 %s register tanpa field 'ip' - push audio TTS "
                                "tidak akan berfungsi. Update firmware agar mengirim "
                                "WiFi.localIP() saat register.", device_id)
                        linked = sessions.ensure_device_session(device_id)
                        safe_ws.send_json({"type": "registered", "device_id": device_id,
                                           "paired_session": linked})
                        self.log.info("ESP32 terdaftar: device_id=%s session=%s", device_id, linked)
                    continue

                if mtype == "pair_device":          # pairing lama: HP/browser <-> ESP32
                    target = msg.get("device_id")
                    if not session_id or not target:
                        safe_ws.send_json({"type": "error", "message": "session/device tidak valid"})
                        continue
                    sessions.link_device(target, session_id)
                    safe_ws.send_json({"type": "paired", "device_id": target})
                    self.push_to_device(target, {"type": "paired", "session_id": session_id})
                    continue

                if mtype in ("button_down", "button_up"):
                    target = sessions.session_for_device(device_id) if device_id else None
                    if target:
                        self.push_to_session(target, {
                            "type": "start_recording" if mtype == "button_down" else "stop_recording"})
                    continue

                if mtype in QUICK_TYPES:
                    target_device = device_id or msg.get("device_id")
                    event = msg.get("event") or msg.get("mode")
                    # Sampel jarak dari firmware esp32_main: bukan tombol, jangan dibalas error.
                    if event == "obstacle_warning" or (event is None and msg.get("button") is None
                                                       and "distance_cm" in msg):
                        distance = msg.get("distance_cm")
                        if target_device and valid_distance(distance):
                            sessions.record_distance(sessions.ensure_device_session(target_device),
                                                     float(distance))
                        continue
                    if not target_device:
                        safe_ws.send_json({"type": "error", "message": "device_id wajib diisi"})
                        continue
                    try:
                        outcome = self.hub.dispatch(event=event, button=msg.get("button"),
                                                    device_id=target_device, source="websocket")
                    except ValueError as exc:
                        safe_ws.send_json({"type": "error", "message": str(exc)})
                        continue
                    safe_ws.send_json({"type": "quick_ack", "status": outcome["status"],
                                       "event": outcome["event"], "button": outcome["button"],
                                       "delivered": outcome["delivered"]})
                    continue

                if mtype == "toggle":
                    target = sessions.session_for_device(device_id) if device_id else None
                    if target:
                        self.push_to_session(target, {"type": "toggle", "state": msg.get("state")})
                    continue

                if mtype == "ultrasonic":
                    if not device_id:
                        continue
                    distance = msg.get("distance_cm")
                    if not valid_distance(distance):
                        continue
                    target = sessions.ensure_device_session(device_id)
                    sessions.record_distance(target, float(distance))
                    text = ultrasonic_message(distance)
                    if text:
                        name = f"ultrasonic_{safe_token(device_id, 'device')}.mp3"
                        mp3_path = self._save_tts(text, name)
                        if mp3_path:
                            esp32_ip = sessions.get_device_ip(device_id)
                            if esp32_ip:
                                self.log.info(
                                    "[ws] ultrasonik -> push audio: device=%s esp32_ip=%s", device_id, esp32_ip)
                                threading.Thread(
                                    target=push_mp3_to_esp32,
                                    args=(mp3_path.read_bytes(), esp32_ip),
                                    daemon=True,
                                ).start()
                            else:
                                self.log.warning(
                                    "Tidak bisa push audio ultrasonik ke %s: IP belum diketahui "
                                    "(ESP32 belum register dengan field 'ip').", device_id)
                        self.push_to_session(target, {"type": "status", "message": text})
                    continue
        except ConnectionClosed:
            pass
        finally:
            if role == "web" and session_id:
                sessions.unregister_web_socket(session_id)
            if role == "esp32" and device_id and sessions.get_esp32_socket(device_id) is safe_ws:
                sessions.unregister_esp32_socket(device_id)   # jangan hapus socket hasil reconnect