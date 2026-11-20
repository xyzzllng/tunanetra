"""Titik masuk tunggal Spider Sense (backend headless).

    python main.py

Tidak membutuhkan browser maupun webcam: tombol fisik ESP32 memicu ESP32-CAM
memotret, AI berjalan di proses ini, dan hasilnya diputar di speaker ESP32.
"""
from __future__ import annotations

import threading

from config.settings import settings
from server.app import create_app


def main():
    app, ctx = create_app()
    cam = ctx.camera.get_status()["source"] if ctx.camera is not None else "BELUM DIKONFIGURASI (isi ESP32CAM_URL)"
    print(f"[Kamera]  {cam}")
    print("[Tombol]  1=Deteksi Objek, 2=Deteksi Ekspresi, 3=Deteksi Rambu Lalu Lintas")
    print(f"[ESP32]   WebSocket ws://<ip-backend>:{settings.port}/ws | POST /api/esp32/event")
    if ctx.serial_bridge is not None:
        print(f"[ESP32]   serial aktif (port={settings.esp32_serial_port})")
    if settings.web_ui_enabled:
        print(f"[Monitor] halaman web opsional: http://localhost:{settings.port}")
    threading.Thread(target=ctx.background_task, daemon=True).start()
    app.run(host=settings.host, port=settings.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
