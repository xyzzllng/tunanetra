"""Perakitan aplikasi Flask (headless): satu-satunya tempat semua layanan dirangkai.

Menggantikan ``server.py`` (webcam tunggal) dan ``server_multidevice.py`` (kamera
di browser) yang sebelumnya menduplikasi banyak logika. Kamera sekarang
ESP32-CAM; pemicunya tombol fisik ESP32; keluarannya speaker ESP32.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from ai.runtime import factory, vision_services
from camera import build_camera
from config.settings import BASE_DIR, settings as default_settings
from devices.events import register_esp32_events
from devices.serial_bridge import start_serial_bridge
from devices.ws import DeviceLink
from server.upload import register_upload
from services.quick_commands import register_quick_commands
from services.sessions import SessionManager

WEB_DIR = BASE_DIR / "web"
_UNSET = object()


@dataclass
class AppContext:
    settings: object
    sessions: SessionManager
    audio: object
    groq: object
    database: object
    vision: dict
    camera: object
    init_errors: dict
    audio_dir: Path
    link: DeviceLink = None
    hub: object = None
    quick: object = None
    serial_bridge: object = None
    extras: dict = field(default_factory=dict)

    def background_task(self):
        """Pembaruan dashboard Ubidots/Mongo + pembersihan sesi, tiap 60 detik."""
        while True:
            try:
                if self.database:
                    self.database.update_background_data()
                self.sessions.cleanup_expired()
            except Exception as exc:
                logging.getLogger(__name__).warning("Error background task: %s", exc)
            time.sleep(60)


def create_app(settings=default_settings, *, camera=_UNSET, audio=_UNSET, groq=_UNSET,
               database=_UNSET, vision=_UNSET, start_serial=True):
    """Bangun aplikasi. Argumen kata-kunci memungkinkan injeksi objek palsu pada test."""
    web_enabled = bool(settings.web_ui_enabled and WEB_DIR.is_dir())
    app = Flask(__name__, static_folder=str(WEB_DIR) if web_enabled else None, static_url_path="")
    # Format dengan timestamp presisi ms + nama thread, supaya bisa dicocokkan
    # langsung dengan timestamp Serial Monitor ESP32 saat debug audio push.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(threadName)s] %(levelname)s:%(name)s:%(message)s",
        datefmt="%H:%M:%S",
    )
    log = app.logger
    init_errors = {}

    def safe_init(label, build):
        try:
            instance = build()
            log.info("%s siap", label)
            return instance
        except Exception as exc:
            init_errors[label] = str(exc)
            log.warning("%s tidak tersedia: %s", label, exc)
            return None

    audio_dir = BASE_DIR / "audio_storage"
    audio_dir.mkdir(exist_ok=True)
    ctx = AppContext(
        settings=settings,
        sessions=SessionManager(session_ttl_seconds=settings.session_ttl_seconds),
        audio=audio if audio is not _UNSET else safe_init("Audio", factory("services.audio", "AudioProcessor")),
        groq=groq if groq is not _UNSET else safe_init("Groq", factory("ai.llm", "GroqAI")),
        database=database if database is not _UNSET else safe_init("Database", factory("services.database", "Database")),
        vision=vision if vision is not _UNSET else vision_services(),
        camera=camera if camera is not _UNSET else build_camera(settings),
        init_errors=init_errors,
        audio_dir=audio_dir,
    )
    app.extensions["ctx"] = ctx

    ctx.hub = register_esp32_events(app, ctx.sessions, settings, logger=log)
    ctx.link = DeviceLink(app, ctx.sessions, ctx.hub, lambda: ctx.audio, audio_dir)
    ctx.quick = register_quick_commands(
        app, ctx.vision, ctx.sessions, lambda: ctx.audio, lambda: ctx.database,
        camera=ctx.camera, push_device=ctx.link.push_to_device, notify=ctx.hub.publish)
    ctx.hub.trigger = ctx.quick.start_from_button
    register_upload(app, ctx)
    if start_serial:
        ctx.serial_bridge = start_serial_bridge(ctx.hub, settings, logger=log)

    # ----------------------------------------------------------- kamera
    @app.get("/camera_status")
    def camera_status():
        if ctx.camera is None:
            return jsonify(status="error", message="Kamera belum dikonfigurasi (ESP32CAM_URL)"), 503
        probe_result = None
        if request.args.get("probe") and hasattr(ctx.camera, "probe"):
            probe_result = ctx.camera.probe()
        camera_info = ctx.camera.get_status()
        if request.args.get("probe") and hasattr(ctx.camera, "probe"):
            camera_info["reachable"] = bool(probe_result and probe_result.get("reachable"))
        return jsonify(status="success", camera=camera_info)

    @app.get("/video_feed")
    def video_feed():
        """Pratinjau manual (MJPEG dari ESP32-CAM). Tidak dipakai oleh alur deteksi."""
        if ctx.camera is None:
            return jsonify(status="error", message="Kamera belum dikonfigurasi"), 503
        return Response(ctx.camera.mjpeg_stream(), mimetype="multipart/x-mixed-replace; boundary=frame")

    # ---------------------------------------------------------- pairing
    @app.post("/api/pair")
    def pair_device():
        data = request.get_json(force=True, silent=True) or {}
        session_id, device_id = data.get("session_id"), data.get("device_id")
        if not session_id or not device_id:
            return jsonify(status="error", message="session_id dan device_id wajib"), 400
        ctx.sessions.create_session(session_id)
        ctx.sessions.link_device(device_id, session_id)
        ctx.link.push_to_device(device_id, {"type": "paired", "session_id": session_id})
        return jsonify(status="success")

    # ----------------------------------------------------------- health
    @app.get("/api/health")
    def health():
        vision_status = {name: svc.status() for name, svc in ctx.vision.items()}
        detector = ctx.vision["object"]
        camera_info = {"configured": ctx.camera is not None}
        if ctx.camera is not None:
            probe_result = None
            if hasattr(ctx.camera, "probe"):
                probe_result = ctx.camera.probe()
            camera_info.update(ctx.camera.get_status())
            if hasattr(ctx.camera, "probe"):
                camera_info["reachable"] = bool(probe_result and probe_result.get("reachable"))
        return jsonify(
            status="success",
            camera=camera_info,
            esp32={k: v for k, v in ctx.hub.status().items() if k != "log"},
            services={
                "vision": vision_status,
                "object_detection": detector.instance is not None,
                "object_detection_error": detector.error,
                "object_detection_diagnostics": detector.instance.diagnostics() if detector.instance else {},
                "groq": bool(ctx.groq and ctx.groq.available),
                "stt": bool(ctx.audio and ctx.audio.stt_available),
                "tts": ctx.audio is not None,
                "mongodb": ctx.database.mongo_status() if ctx.database else {"configured": False, "available": False},
                "ubidots_configured": bool(ctx.database and ctx.database.UBIDOTS_TOKEN),
                "init_errors": ctx.init_errors,
            })

    if web_enabled:
        @app.get("/")
        def index():
            return send_from_directory(WEB_DIR, "index.html")

    return app, ctx