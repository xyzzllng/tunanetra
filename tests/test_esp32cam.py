"""Adapter ESP32-CAM diuji terhadap server HTTP palsu yang meniru firmware MicroPython
di firmware/esp32_cam/main.py: SETIAP path (dan method GET apa pun) dibalas satu JPEG
segar, tanpa endpoint /capture, /status, tanpa token, tanpa parameter flash.
"""
import threading

import cv2
import numpy as np
import pytest
from flask import Flask, Response
from werkzeug.serving import make_server

from camera import CameraError, build_camera
from camera.esp32cam import Esp32Cam


def jpeg(seed=0, size=(320, 240)):
    img = np.full((size[1], size[0], 3), 90 + seed, np.uint8)
    cv2.putText(img, f"frame{seed}", (10, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    return cv2.imencode(".jpg", img)[1].tobytes()


class FakeCam:
    """Server tiruan yang meniru perilaku firmware/esp32_cam/main.py: request apa pun
    (path apa pun) dibalas satu JPEG baru dari "kamera" — persis seperti firmware asli
    yang tidak membaca path/method dari request sama sekali.
    """

    def __init__(self):
        self.hits, self.fail_first, self.body, self.size = 0, 0, None, (320, 240)
        app = Flask(__name__)

        @app.route("/", defaults={"path": ""})
        @app.route("/<path:path>")
        def catch_all(path):
            self.hits += 1
            if self.fail_first > 0:
                self.fail_first -= 1
                return Response("camera busy", 503)
            return Response(self.body if self.body is not None else jpeg(self.hits, self.size),
                            mimetype="image/jpeg")

        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def cam_server():
    server = FakeCam()
    yield server
    server.close()


def test_burst_returns_same_size_fresh_frames(cam_server):
    cam = Esp32Cam(cam_server.url, burst_gap_ms=0)
    frames = cam.capture_frames(3)
    assert len(frames) == 3 and all(f.shape == (240, 320, 3) for f in frames)
    assert cam_server.hits == 3                                   # tidak memakai cache
    assert cam.get_status()["available"] and cam.get_status()["frame_size"] == [320, 240]


def test_count_is_clamped_to_three(cam_server):
    assert len(Esp32Cam(cam_server.url, burst_gap_ms=0).capture_frames(9)) == 3


def test_capture_picks_a_frame_and_get_frame(cam_server):
    cam = Esp32Cam(cam_server.url, burst_gap_ms=0, best_of=2)
    assert cam.capture("rambu").shape == (240, 320, 3)
    assert cam.get_frame() is not None


def test_retries_transient_errors(cam_server):
    cam_server.fail_first = 2
    cam = Esp32Cam(cam_server.url, retries=2, burst_gap_ms=0)
    assert len(cam.capture_frames(1)) == 1


def test_gives_up_after_retries(cam_server):
    cam_server.fail_first = 99
    cam = Esp32Cam(cam_server.url, retries=1, burst_gap_ms=0)
    with pytest.raises(CameraError):
        cam.capture_frames(1)
    assert cam.get_frame() is None and cam.capture() is None
    assert not cam.get_status()["available"] and cam.get_status()["last_error"]


def test_unreachable_camera_raises_camera_error():
    cam = Esp32Cam("http://127.0.0.1:9", timeout=1, retries=0)
    with pytest.raises(CameraError):
        cam.capture_frames(1)


def test_non_jpeg_response_rejected(cam_server):
    cam_server.body = b"<html>not a jpeg</html>" * 50
    with pytest.raises(CameraError):
        Esp32Cam(cam_server.url, retries=0).capture_frames(1)


def test_token_and_flash_kwargs_accepted_but_ignored(cam_server):
    """Firmware ini tidak punya otentikasi/LED flash; parameter tetap diterima
    (kompatibilitas dengan config/settings.py) tapi tidak memengaruhi request."""
    cam = Esp32Cam(cam_server.url, token="tidak-diperiksa", flash=True, burst_gap_ms=0)
    frames = cam.capture_frames(1)
    assert len(frames) == 1


def test_any_path_returns_a_fresh_frame(cam_server):
    """Ciri khas firmware ini: path apa pun (bukan hanya root) tetap dibalas JPEG baru."""
    cam = Esp32Cam(cam_server.url, burst_gap_ms=0)
    cam.base_url = cam.base_url  # root dipakai secara internal; path lain diuji langsung:
    import requests
    resp = requests.get(cam_server.url + "/path/acak/tidak/dikenal")
    assert resp.status_code == 200 and resp.content[:2] == b"\xff\xd8"


def test_resolution_change_inside_burst_is_rejected(cam_server):
    cam = Esp32Cam(cam_server.url, burst_gap_ms=0)
    original = cam._fetch_jpeg
    calls = {"n": 0}

    def flip():
        calls["n"] += 1
        cam_server.size = (320, 240) if calls["n"] == 1 else (160, 120)
        return original()
    cam._fetch_jpeg = flip
    with pytest.raises(CameraError, match="Resolusi"):
        cam.capture_frames(2)


def test_probe_checks_reachability_only(cam_server):
    """Firmware ini tidak punya /status JSON; probe() hanya memastikan server membalas JPEG."""
    assert Esp32Cam(cam_server.url).probe() == {"reachable": True}


def test_probe_returns_none_when_unreachable():
    assert Esp32Cam("http://127.0.0.1:9", timeout=1).probe(timeout=0.5) is None


def test_mjpeg_stream_passes_jpeg_through(cam_server):
    chunk = next(Esp32Cam(cam_server.url).mjpeg_stream(max_fps=50))
    assert chunk.startswith(b"--frame") and b"\xff\xd8" in chunk


def test_missing_url_is_a_configuration_error():
    with pytest.raises(CameraError, match="ESP32CAM_URL"):
        Esp32Cam("")


def test_build_camera_handles_missing_url_and_unknown_source(cam_server):
    from dataclasses import replace
    from config.settings import settings
    assert build_camera(replace(settings, camera_source="esp32cam", esp32cam_url="")) is None
    assert build_camera(replace(settings, camera_source="tidak-dikenal")) is None
    built = build_camera(replace(settings, camera_source="esp32cam", esp32cam_url=cam_server.url))
    assert isinstance(built, Esp32Cam)
