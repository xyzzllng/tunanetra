"""Uji ujung-ke-ujung lewat jaringan sungguhan (loopback):

  klien WebSocket (meniru firmware esp32_main) --> backend --> HTTP ke ESP32-CAM palsu
  --> AI palsu --> TTS palsu --> pesan tts_ready --> ESP32 mengunduh MP3 lewat HTTP.

Model AI dan TTS diganti objek palsu (torch/Groq tidak dibutuhkan); yang diuji adalah
seluruh jalur komunikasinya.
"""
import io
import json
import threading
from unittest.mock import Mock

import pytest
import requests
from simple_websocket import Client
from werkzeug.serving import make_server

from camera.esp32cam import Esp32Cam
from server.app import create_app
from tests.test_esp32cam import FakeCam


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setattr("server.app.BASE_DIR", tmp_path)
    monkeypatch.setattr("services.quick_commands.BASE_DIR", tmp_path)
    cam_server = FakeCam()
    box = {"label": "chair", "confidence": .8, "box": [200, 100, 300, 300], "area_ratio": .2}
    vision = {
        "object": Mock(detect_boxes=Mock(return_value=[dict(box)]), diagnostics=Mock(return_value={}),
                       summarize=Mock(return_value=["kursi"])),
        "expression": Mock(), "sign": Mock(), "scene": Mock(detect_boxes=Mock(return_value=[])),
    }
    audio = Mock(generate_tts=Mock(side_effect=lambda t: io.BytesIO(b"ID3-fake-mp3:" + t.encode())),
                 _play_audio=Mock(return_value=False), stt_available=False)
    app, ctx = create_app(camera=Esp32Cam(cam_server.url, burst_gap_ms=0), audio=audio, groq=None,
                          database=Mock(mongo_status=Mock(return_value={"available": False}), UBIDOTS_TOKEN=""),
                          vision=vision, start_serial=False)
    ctx.hub.cooldown_ms = 0
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_port}", cam_server, vision, audio
    server.shutdown()
    cam_server.close()


def recv(ws, kind, timeout=8):
    while True:
        data = ws.receive(timeout=timeout)
        assert data is not None, f"timeout menunggu pesan '{kind}'"
        message = json.loads(data)
        if message.get("type") == kind:
            return message


def test_button_press_over_websocket_ends_with_speech_on_esp32(stack):
    host, cam_server, vision, audio = stack
    ws = Client.connect(f"ws://{host}/ws")
    try:
        ws.send(json.dumps({"type": "register", "role": "esp32", "device_id": "esp32-A1"}))
        assert recv(ws, "registered")["paired_session"] == "dev-esp32-A1"

        ws.send(json.dumps({"type": "quick_command", "event": "object_detection", "button": 1,
                            "device_id": "esp32-A1"}))
        ack = recv(ws, "quick_ack")
        assert ack["delivered"] == 1                                   # firmware: beep konfirmasi

        tts = recv(ws, "tts_ready")
        assert tts["url"].startswith("/api/quick_audio_file/")
        assert cam_server.hits == 3                                    # 3 frame dari ESP32-CAM
        mp3 = requests.get(f"http://{host}{tts['url']}", timeout=5)    # ESP32 mengunduh MP3
        assert mp3.status_code == 200 and mp3.headers["Content-Type"] == "audio/mpeg"
        assert b"kursi" in mp3.content                                 # hasil AI yang diucapkan
    finally:
        ws.close()


def test_camera_down_is_spoken_on_esp32(stack):
    host, cam_server, vision, audio = stack
    cam_server.fail_first = 10_000
    ws = Client.connect(f"ws://{host}/ws")
    try:
        ws.send(json.dumps({"type": "register", "role": "esp32", "device_id": "esp32-A1"}))
        recv(ws, "registered")
        ws.send(json.dumps({"type": "quick_command", "event": "text_detection", "device_id": "esp32-A1"}))
        tts = recv(ws, "tts_ready", timeout=20)
        mp3 = requests.get(f"http://{host}{tts['url']}", timeout=5)
        assert b"Kamera tidak terhubung" in mp3.content
    finally:
        ws.close()


def test_obstacle_samples_flow_without_errors_and_feed_object_summary(stack):
    host, cam_server, vision, audio = stack
    ws = Client.connect(f"ws://{host}/ws")
    try:
        ws.send(json.dumps({"type": "register", "role": "esp32", "device_id": "esp32-A1"}))
        recv(ws, "registered")
        ws.send(json.dumps({"type": "quick_command", "event": "obstacle_warning", "distance_cm": 35.0,
                            "device_id": "esp32-A1"}))
        ws.send(json.dumps({"type": "quick_command", "event": "object_detection", "device_id": "esp32-A1"}))
        assert recv(ws, "quick_ack")["delivered"] == 1                 # ack pertama = tombol, bukan error
        recv(ws, "tts_ready")
        assert "sensor membaca halangan sekitar 35 cm" in audio.generate_tts.call_args.args[0]
    finally:
        ws.close()


def test_health_reports_camera_and_esp32(stack):
    host, *_ = stack
    for name, svc in stack[2].items():
        svc.status = Mock(return_value={"state": "ready"})
    stack[2]["object"].instance, stack[2]["object"].error = None, None
    data = requests.get(f"http://{host}/api/health", timeout=5).json()
    assert data["camera"]["configured"] and data["camera"]["source"].startswith("esp32cam://")
    assert data["esp32"]["trigger_ready"] is True
