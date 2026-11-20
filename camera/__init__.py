"""Sumber gambar Spider Sense.

Semua sumber kamera memakai antarmuka yang sama, sehingga ``services/quick_commands``
dan ``server/upload`` tidak perlu tahu kamera apa yang dipakai::

    capture_frames(count) -> list[ndarray BGR]   burst pendek (maks. 3), semua frame berukuran sama
    capture(mode)         -> ndarray | None      satu frame terbaik; None bila gagal
    get_frame()           -> ndarray | None
    mjpeg_stream(max_fps) -> generator multipart untuk pratinjau manual
    get_status()          -> dict ringkas untuk /api/health
    release()

Sumber yang tersedia:

* ``esp32cam`` (default) - ``camera/esp32cam.py``: menarik JPEG dari firmware ``firmware/esp32_cam``.
* ``webcam``  (opsional, hanya uji tanpa perangkat keras) - ``camera/webcam.py``.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    """Kamera tidak dapat dihubungi atau tidak menghasilkan gambar yang valid."""


def build_camera(settings):
    """Bangun objek kamera sesuai ``CAMERA_SOURCE``; kembalikan ``None`` bila gagal.

    Kegagalan konfigurasi tidak menjatuhkan server: perintah tombol akan
    mengumumkan "kamera belum dikonfigurasi" alih-alih membuat proses mati.
    """
    source = (settings.camera_source or "esp32cam").lower()
    if source == "esp32cam":
        from camera.esp32cam import Esp32Cam

        try:
            return Esp32Cam(
                settings.esp32cam_url,
                token=settings.esp32cam_token,
                timeout=settings.esp32cam_timeout_seconds,
                retries=settings.esp32cam_retries,
                burst_gap_ms=settings.esp32cam_burst_gap_ms,
                best_of=settings.esp32cam_best_of,
                flash=settings.esp32cam_flash,
            )
        except CameraError as exc:
            log.warning("ESP32-CAM tidak dikonfigurasi: %s", exc)
            return None
    if source == "webcam":
        from camera.webcam import Camera

        try:
            return Camera()
        except Exception as exc:
            log.warning("Webcam (mode uji) tidak tersedia: %s", exc)
            return None
    log.warning("CAMERA_SOURCE='%s' tidak dikenali; gunakan esp32cam atau webcam.", source)
    return None
