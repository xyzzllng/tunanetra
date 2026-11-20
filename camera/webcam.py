"""On-demand webcam adapter for Spider Sense.

The camera handle stays ready, but frames are NOT read continuously in a
background thread. A short burst is captured only when ``capture()`` is called
by a user request. ``mjpeg_stream()`` remains available as an explicit manual
preview endpoint and is the only path that intentionally reads frames
continuously.
"""

import os
import platform
import threading
import time
from datetime import datetime

import cv2


class Camera:
    """External webcam with prompt-triggered, best-frame capture."""

    def __init__(self, source=None):
        raw_source = os.getenv("WEBCAM_SOURCE", "0") if source is None else source
        self.source = self._parse_source(raw_source)
        self.width = int(os.getenv("WEBCAM_WIDTH", "1280"))
        self.height = int(os.getenv("WEBCAM_HEIGHT", "720"))
        self.fps = int(os.getenv("WEBCAM_FPS", "30"))
        self.capture_burst = max(1, int(os.getenv("WEBCAM_CAPTURE_BURST", "3")))
        self.capture_gap_ms = max(0, int(os.getenv("WEBCAM_CAPTURE_GAP_MS", "20")))
        self.save_captures = os.getenv("SAVE_CAPTURES", "false").lower() in ("1", "true", "yes", "on")

        default_capture_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "captured_images",
        )
        self.SAVE_DIR = os.getenv("CAPTURE_DIR", default_capture_dir)
        self.CAMERA_IP = f"webcam://{self.source}"
        self.SOURCE_LABEL = self.CAMERA_IP

        self._capture = None
        self._capture_lock = threading.RLock()
        self._last_frame = None
        self._last_frame_time = 0.0
        self._last_error = None

        os.makedirs(self.SAVE_DIR, exist_ok=True)
        self._open_camera()

        # Read only a few frames once at startup to validate the device. There
        # is deliberately no background capture thread after this point.
        frame = self._capture_best_frame(max_frames=min(2, self.capture_burst))
        if frame is None:
            self.release()
            raise RuntimeError(
                f"Webcam tidak dapat menghasilkan frame dari source {self.source}. "
                "Coba set WEBCAM_SOURCE=0, 1, 2, ... sesuai index webcam."
            )

    @staticmethod
    def _parse_source(source):
        if isinstance(source, int):
            return source
        source = str(source).strip()
        try:
            return int(source)
        except ValueError:
            return source

    def _create_capture(self):
        if isinstance(self.source, int) and platform.system() == "Windows":
            cap = cv2.VideoCapture(self.source, cv2.CAP_DSHOW)
            if cap.isOpened():
                return cap
            cap.release()
        return cv2.VideoCapture(self.source, cv2.CAP_ANY)

    def _open_camera(self):
        with self._capture_lock:
            if self._capture is not None:
                self._capture.release()
            self._capture = self._create_capture()
            if not self._capture.isOpened():
                raise RuntimeError(f"Webcam source {self.source} tidak dapat dibuka")
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self._capture.set(cv2.CAP_PROP_FPS, self.fps)
            self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    @staticmethod
    def _sharpness(frame):
        if frame is None or not getattr(frame, "size", 0):
            return -1.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def _capture_best_frame(self, max_frames=None):
        """Flush a tiny burst and return the sharpest frame.

        This avoids continuously reading 30 FPS while still discarding a stale
        or blurred first frame after the camera has been idle.
        """
        count = max(1, int(max_frames or self.capture_burst))
        best = None
        best_score = -1.0

        with self._capture_lock:
            if self._capture is None or not self._capture.isOpened():
                try:
                    # We already hold the lock, so reopen inline instead of
                    # recursively calling _open_camera().
                    self._capture = self._create_capture()
                    if not self._capture.isOpened():
                        self._last_error = "Webcam tidak dapat dibuka kembali"
                        return None
                    self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                    self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                    self._capture.set(cv2.CAP_PROP_FPS, self.fps)
                    self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception as exc:
                    self._last_error = str(exc)
                    return None

            for index in range(count):
                ok, frame = self._capture.read()
                if ok and frame is not None and frame.size:
                    score = self._sharpness(frame)
                    if score > best_score:
                        best = frame.copy()
                        best_score = score
                if index + 1 < count and self.capture_gap_ms:
                    time.sleep(self.capture_gap_ms / 1000.0)

        if best is not None:
            self._last_frame = best.copy()
            self._last_frame_time = time.time()
            self._last_error = None
            return best

        self._last_error = "Gagal membaca frame webcam"
        return None

    def get_frame(self):
        """Read one fresh frame on demand; no background FPS loop is used."""
        return self._capture_best_frame(max_frames=1)

    def capture_frames(self, count=3):
        """Atomic fresh burst shared with preview: no second VideoCapture."""
        frames = []
        with self._capture_lock:
            if self._capture is None or not self._capture.isOpened():
                self._open_camera()
            # Drop queued frames after idle; never return an old cached frame.
            for _ in range(4):
                self._capture.grab()
            for index in range(min(3, max(1, int(count)))):
                ok, frame = self._capture.read()
                if not ok or frame is None or not frame.size:
                    self._last_error = "Webcam terputus saat mengambil burst"
                    self.release()
                    raise RuntimeError(self._last_error)
                frames.append(frame.copy())
                if index+1 < count:
                    time.sleep(.10)
            self._last_frame = frames[-1].copy()
            self._last_frame_time = time.time()
            self._last_error = None
        return frames

    def capture(self, mode="capture"):
        """Capture a short burst only when a user action requests a photo."""
        frame = self._capture_best_frame()
        if frame is None:
            # One recovery attempt for a temporarily disconnected USB camera.
            try:
                self._open_camera()
                frame = self._capture_best_frame()
            except Exception as exc:
                self._last_error = str(exc)
                return None
        if frame is None:
            return None

        if self.save_captures:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            safe_mode = "".join(c for c in str(mode) if c.isalnum() or c in ("-", "_")) or "capture"
            filepath = os.path.join(self.SAVE_DIR, f"{safe_mode}_{timestamp}.jpg")
            cv2.imwrite(filepath, frame)
        return frame

    def mjpeg_stream(self, max_fps=10):
        """Explicit live preview. Continuous reads happen only while viewed."""
        interval = 1.0 / max(1, int(max_fps))
        while True:
            frame = self.get_frame()
            if frame is None:
                time.sleep(0.1)
                continue
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"
                )
            time.sleep(interval)

    def is_camera_available(self):
        with self._capture_lock:
            return bool(self._capture is not None and self._capture.isOpened())

    def get_status(self):
        return {
            "available": self.is_camera_available(),
            "source": self.SOURCE_LABEL,
            "mode": "on_demand",
            "configured_width": self.width,
            "configured_height": self.height,
            "configured_fps": self.fps,
            "capture_burst": self.capture_burst,
            "last_capture_age_ms": (
                int((time.time() - self._last_frame_time) * 1000)
                if self._last_frame_time else None
            ),
            "last_error": self._last_error,
        }

    def release(self):
        with self._capture_lock:
            if self._capture is not None:
                self._capture.release()
                self._capture = None

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass
