"""Adapter kamera ESP32-CAM — firmware MicroPython (``firmware/esp32_cam/main.py``).

Diadaptasi dari ``iot tunanetra (2)/fix new - Copy/utils/camera.py`` (class
``Camera``) supaya cocok dengan firmware MicroPython yang benar-benar dipakai
dan sudah teruji, bukan sketsa Arduino di ``firmware/esp32_cam/EXCEPT``
(belum pernah diuji di perangkat nyata — lihat README di folder tersebut).

Firmware MicroPython ini SANGAT sederhana: server-nya tidak membaca method
maupun path dari request HTTP sama sekali — setiap koneksi TCP yang masuk
dibalas dengan satu JPEG segar hasil ``camera.capture()``. Konsekuensinya:

* Tidak ada endpoint ``/capture`` atau ``/status`` terpisah — semua path
  (termasuk ``/``) menghasilkan foto baru.
* Tidak ada pemeriksaan token/otentikasi.
* Tidak ada kontrol LED flash dari sisi backend.

``ESP32CAM_TOKEN`` dan ``ESP32CAM_FLASH`` di ``.env`` karena itu diterima
untuk kompatibilitas tapi diabaikan (dicatat sekali di log).

Antarmuka publik sengaja identik dengan implementasi ESP32-CAM sebelumnya /
``camera.webcam.Camera`` sehingga ``services/quick_commands`` dan
``server/app.py`` tidak perlu tahu apa pun tentang detail ini.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

import cv2
import numpy as np
import requests

from camera import CameraError

log = logging.getLogger(__name__)


class Esp32Cam:
    """Kamera ESP32-CAM (MicroPython) yang dibaca sesuai permintaan (on-demand)."""

    def __init__(self, base_url, *, token="", timeout=6.0, retries=2, burst_gap_ms=120,
                 best_of=2, flash=False, http=None):
        base_url = (base_url or "").strip().rstrip("/")
        if not base_url:
            raise CameraError("ESP32CAM_URL belum diisi (contoh: http://192.168.1.60).")
        if not base_url.startswith(("http://", "https://")):
            base_url = "http://" + base_url
        self.base_url = base_url
        self.timeout = max(1.0, float(timeout))
        self.retries = max(0, int(retries))
        self.burst_gap = max(0, int(burst_gap_ms)) / 1000.0
        self.best_of = max(1, min(3, int(best_of)))
        if token:
            log.info("ESP32CAM_TOKEN diisi, tapi firmware MicroPython ini tidak memeriksa token; diabaikan.")
        if flash:
            log.info("ESP32CAM_FLASH=true, tapi firmware MicroPython ini tidak punya kontrol LED; diabaikan.")
        self.SOURCE_LABEL = f"esp32cam://{urlsplit(base_url).netloc}"
        self.CAMERA_IP = self.SOURCE_LABEL  # nama lama, dipakai beberapa log/pesan suara

        self.save_captures = os.getenv("SAVE_CAPTURES", "false").lower() in ("1", "true", "yes", "on")
        self.SAVE_DIR = os.getenv("CAPTURE_DIR", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captured_images"))

        self._http = http or requests.Session()
        self._lock = threading.RLock()
        self._last_frame_time = 0.0
        self._last_error = None
        self._last_latency_ms = None
        self._last_shape = None
        self._failures = 0

    # ------------------------------------------------------------------ HTTP
    def _fetch_jpeg(self):
        """Ambil satu JPEG mentah dari root firmware. Mencoba ulang untuk galat jaringan sementara."""
        last = None
        for attempt in range(self.retries + 1):
            try:
                started = time.monotonic()
                response = self._http.get(self.base_url + "/", timeout=(self.timeout, self.timeout))
                if response.status_code != 200:
                    raise requests.RequestException(f"HTTP {response.status_code}")
                raw = response.content
                if not raw:
                    raise requests.RequestException("respons kosong")
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) != len(raw):
                    raise requests.RequestException(f"JPEG terpotong ({len(raw)} dari {declared} byte)")
                if len(raw) < 500 or raw[:2] != b"\xff\xd8":
                    raise requests.RequestException("respons bukan JPEG yang valid")
                self._last_latency_ms = round((time.monotonic() - started) * 1000)
                return raw
            except requests.RequestException as exc:
                last = exc
                log.debug("Percobaan %s gagal ambil frame ESP32-CAM: %s", attempt + 1, exc)
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        raise CameraError(f"ESP32-CAM tidak dapat dihubungi di {self.base_url}: {last}")

    @staticmethod
    def _decode(raw):
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or not frame.size:
            raise CameraError("JPEG dari ESP32-CAM rusak dan tidak dapat didekode.")
        return frame

    @staticmethod
    def _sharpness(frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def _grab_locked(self):
        try:
            frame = self._decode(self._fetch_jpeg())
        except CameraError as exc:
            self._failures += 1
            self._last_error = str(exc)
            raise
        self._failures = 0
        self._last_error = None
        self._last_frame_time = time.time()
        self._last_shape = [int(frame.shape[1]), int(frame.shape[0])]
        if self.save_captures:
            try:
                os.makedirs(self.SAVE_DIR, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                cv2.imwrite(os.path.join(self.SAVE_DIR, f"esp32cam_{stamp}.jpg"), frame)
            except OSError:
                pass
        return frame

    # ----------------------------------------------- antarmuka seperti Camera
    def get_frame(self):
        """Satu frame segar, atau ``None`` bila kamera tidak terjangkau."""
        with self._lock:
            try:
                return self._grab_locked()
            except CameraError:
                return None

    def capture_frames(self, count=3):
        """Burst pendek (maks. 3 frame, semua berukuran sama). Melempar ``CameraError``."""
        count = max(1, min(3, int(count)))
        frames = []
        with self._lock:
            for index in range(count):
                frames.append(self._grab_locked())
                if index + 1 < count and self.burst_gap:
                    time.sleep(self.burst_gap)
        if any(f.shape != frames[0].shape for f in frames):
            self._last_error = "Resolusi ESP32-CAM berubah di tengah burst"
            raise CameraError(self._last_error)
        return frames

    def capture(self, mode="capture"):
        """Satu frame paling tajam dari ``best_of`` percobaan; ``None`` bila gagal."""
        best, best_score = None, -1.0
        with self._lock:
            for index in range(self.best_of):
                try:
                    frame = self._grab_locked()
                except CameraError:
                    break
                score = self._sharpness(frame)
                if score > best_score:
                    best, best_score = frame, score
                if index + 1 < self.best_of and self.burst_gap:
                    time.sleep(self.burst_gap)
        return best

    def mjpeg_stream(self, max_fps=4):
        """Pratinjau manual: JPEG diteruskan apa adanya (tanpa dekode) sebagai multipart."""
        interval = 1.0 / max(1, int(max_fps))
        while True:
            started = time.monotonic()
            try:
                with self._lock:
                    raw = self._fetch_jpeg()
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + raw + b"\r\n"
            except CameraError as exc:
                self._last_error = str(exc)
                time.sleep(1.0)
                continue
            time.sleep(max(0.0, interval - (time.monotonic() - started)))

    # ---------------------------------------------------------------- status
    def probe(self, timeout=1.5):
        """Firmware ini tidak punya endpoint /status; sekadar cek server membalas dengan JPEG."""
        try:
            response = self._http.get(self.base_url + "/", timeout=(min(1.5, timeout), timeout))
            reachable = response.status_code == 200 and response.content[:2] == b"\xff\xd8"
            return {"reachable": reachable}
        except requests.RequestException as exc:
            self._last_error = f"tidak terjangkau: {exc}"
            return None

    def is_camera_available(self):
        return self._last_frame_time > 0 and self._failures == 0

    def get_status(self):
        """Status ringkas tanpa akses jaringan."""
        return {
            "available": self.is_camera_available(),
            "source": self.SOURCE_LABEL,
            "mode": "on_demand",
            "frame_size": self._last_shape,
            "latency_ms": self._last_latency_ms,
            "consecutive_failures": self._failures,
            "last_capture_age_ms": (int((time.time() - self._last_frame_time) * 1000)
                                    if self._last_frame_time else None),
            "last_error": self._last_error,
        }

    def release(self):
        try:
            self._http.close()
        except Exception:
            pass