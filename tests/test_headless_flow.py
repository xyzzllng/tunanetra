"""Alur mandiri: tombol ESP32 -> ESP32-CAM -> AI -> TTS -> speaker ESP32 (tanpa browser)."""
import io
import json
import threading
import time
from unittest.mock import Mock

import numpy as np
import pytest

from camera import CameraError
from server.app import create_app


class FakeDeviceSocket:
    """Menggantikan koneksi WebSocket ESP32: menampung semua pesan dari server."""

    def __init__(self):
        self.sent = []

    def send_json(self, payload):
        self.sent.append(payload)
        return True

    def of_type(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


class FakeWS:
    """Objek ws untuk DeviceLink._endpoint (receive/send seperti simple_websocket)."""

    def __init__(self, messages):
        self.inbox = [json.dumps(m) for m in messages]
        self.outbox = []

    def receive(self):
        return self.inbox.pop(0) if self.inbox else None

    def send(self, data):
        self.outbox.append(json.loads(data))


def wait_for(predicate, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def box():
    return {"label": "chair", "confidence": .8, "box": [200, 200, 200, 200], "area_ratio": .13}


@pytest.fixture
def system(tmp_path, monkeypatch, frame):
    monkeypatch.setattr("server.app.BASE_DIR", tmp_path)
    monkeypatch.setattr("services.quick_commands.BASE_DIR", tmp_path)
    vision = {
        "object": Mock(detect_boxes=Mock(return_value=[box()]), diagnostics=Mock(return_value={}),
                       summarize=Mock(return_value=["kursi"]), instance=None, error=None),
        "expression": Mock(detect_faces=Mock(return_value=[]), summarize=Mock(return_value="Wajah terlihat netral.")),
        "sign": Mock(analyze=Mock(return_value={"text": "", "confidence": 0.0, "reliable": False, "boxes": []})),
        "scene": Mock(detect_boxes=Mock(return_value=[])),
    }
    audio = Mock(generate_tts=Mock(side_effect=lambda text: io.BytesIO(b"mp3:" + text.encode())),
                 _play_audio=Mock(return_value=False), stt_available=False)
    camera = Mock(capture_frames=Mock(side_effect=lambda n: [frame.copy() for _ in range(n)]),
                  capture=Mock(return_value=frame.copy()))
    database = Mock()
    app, ctx = create_app(camera=camera, audio=audio, groq=None, database=database, vision=vision,
                          start_serial=False)
    ctx.hub.cooldown_ms = 0
    device = FakeDeviceSocket()
    ctx.sessions.register_esp32_socket("esp-1", device)
    ctx.sessions.ensure_device_session("esp-1")
    return app, ctx, device, camera, vision, audio, database


def test_physical_button_runs_detection_and_speaks_on_esp32(system):
    app, ctx, device, camera, vision, audio, database = system
    outcome = ctx.hub.dispatch(button=1, device_id="esp-1", source="websocket")
    assert outcome["status"] == "started" and outcome["delivered"] == 1
    assert wait_for(lambda: device.of_type("tts_ready"))
    camera.capture_frames.assert_called_once_with(3)              # ESP32-CAM, bukan browser
    assert vision["object"].detect_boxes.call_count == 3
    message = device.of_type("tts_ready")[0]
    assert message["url"].startswith("/api/quick_audio_file/")
    assert app.test_client().get(message["url"]).status_code == 200    # ESP32 bisa mengambil MP3
    assert wait_for(lambda: database.save_interaction.called)


@pytest.mark.parametrize("button,expected", [(2, "expression"), (3, "sign")])
def test_each_button_runs_only_its_own_detector(system, button, expected):
    app, ctx, device, camera, vision, *_ = system
    ctx.hub.dispatch(button=button, device_id="esp-1", source="websocket")
    assert wait_for(lambda: device.of_type("tts_ready"))
    camera.capture_frames.assert_called_once_with(3 if button == 2 else 1)
    assert getattr(vision[expected], "mock_calls")
    others = {"object", "expression", "sign"} - {expected}
    assert not any(vision[name].mock_calls for name in others)


def test_second_press_while_busy_is_rejected_not_queued(system):
    app, ctx, device, camera, *_ = system
    entered, release = threading.Event(), threading.Event()

    def slow(count):
        entered.set()
        assert release.wait(4)
        return [np.full((480, 640, 3), 128, np.uint8) for _ in range(count)]
    camera.capture_frames.side_effect = slow
    assert ctx.hub.dispatch(button=1, device_id="esp-1")["delivered"] == 1
    assert entered.wait(2)
    busy = ctx.hub.dispatch(button=2, device_id="esp-1")
    assert busy["status"] == "busy" and busy["delivered"] == 0
    release.set()
    assert wait_for(lambda: device.of_type("tts_ready"))
    assert wait_for(lambda: ctx.hub.dispatch(button=3, device_id="esp-1")["delivered"] == 1)


def test_camera_failure_is_announced_by_voice(system):
    app, ctx, device, camera, vision, audio, _ = system
    camera.capture_frames.side_effect = CameraError("ESP32-CAM tidak dapat dihubungi")
    ctx.hub.dispatch(button=1, device_id="esp-1")
    assert wait_for(lambda: device.of_type("tts_ready"))
    spoken = audio.generate_tts.call_args.args[0]
    assert "Kamera tidak terhubung" in spoken
    assert not vision["object"].detect_boxes.called
    # Kegagalan tidak mengunci perintah berikutnya.
    camera.capture_frames.side_effect = None
    camera.capture_frames.return_value = [np.full((480, 640, 3), 128, np.uint8)] * 3
    assert wait_for(lambda: ctx.hub.dispatch(button=1, device_id="esp-1")["delivered"] == 1)


def test_failure_falls_back_to_beep_when_tts_is_down(system):
    app, ctx, device, camera, vision, audio, _ = system
    audio.generate_tts.side_effect = RuntimeError("offline")
    camera.capture_frames.side_effect = CameraError("mati")
    ctx.hub.dispatch(button=1, device_id="esp-1")
    assert wait_for(lambda: device.of_type("error"))


def test_without_esp32_socket_audio_plays_locally(system):
    app, ctx, device, camera, vision, audio, _ = system
    ctx.sessions.unregister_esp32_socket("esp-1")
    ctx.hub.dispatch(button=1, device_id="esp-1")
    assert wait_for(lambda: audio._play_audio.called)


def test_websocket_button_gets_ack_and_registers_device(system):
    app, ctx, device, camera, *_ = system
    ctx.sessions.unregister_esp32_socket("esp-1")
    ws = FakeWS([{"type": "register", "role": "esp32", "device_id": "esp-1"},
                 {"type": "quick_command", "event": "object_detection", "button": 1, "device_id": "esp-1"}])
    ctx.link._endpoint(ws)
    kinds = [m["type"] for m in ws.outbox]
    assert kinds == ["registered", "quick_ack"]
    assert ws.outbox[0]["paired_session"] == "dev-esp-1"
    assert ws.outbox[1]["delivered"] == 1
    assert wait_for(lambda: camera.capture_frames.called)


def test_obstacle_warning_records_distance_without_error_reply(system):
    app, ctx, device, camera, *_ = system
    ws = FakeWS([{"type": "register", "role": "esp32", "device_id": "esp-1"},
                 {"type": "quick_command", "event": "obstacle_warning", "distance_cm": 42.5, "device_id": "esp-1"},
                 {"type": "quick_command", "event": "obstacle_warning", "distance_cm": 9999, "device_id": "esp-1"}])
    ctx.link._endpoint(ws)
    assert [m["type"] for m in ws.outbox] == ["registered"]        # tidak ada "error" -> tidak ada beep salah
    assert ctx.sessions.get("dev-esp-1")["distance_samples"][-1][1] == 42.5
    assert len(ctx.sessions.get("dev-esp-1")["distance_samples"]) == 1
    assert not camera.capture_frames.called


def test_recorded_sensor_distance_reaches_object_summary(system):
    app, ctx, device, *_ = system
    ctx.sessions.record_distance("dev-esp-1", 40)
    ctx.hub.dispatch(button=1, device_id="esp-1")
    assert wait_for(lambda: device.of_type("tts_ready"))
    spoken = system[5].generate_tts.call_args.args[0]
    assert "sensor membaca halangan sekitar 40 cm" in spoken


def test_unknown_button_is_rejected_with_error(system):
    app, ctx, device, *_ = system
    ws = FakeWS([{"type": "register", "role": "esp32", "device_id": "esp-1"},
                 {"type": "quick_command", "button": 7}])
    ctx.link._endpoint(ws)
    assert ws.outbox[-1]["type"] == "error"


def test_http_event_endpoint_starts_detection(system):
    app, ctx, device, camera, *_ = system
    response = app.test_client().post("/api/esp32/event", json={"button": 3, "device_id": "esp-1"})
    assert response.status_code == 200 and response.json["status"] == "started"
    assert wait_for(lambda: device.of_type("tts_ready"))


def test_upload_text_question_uses_esp32_cam_and_speaks_on_device(system):
    app, ctx, device, camera, vision, audio, _ = system
    response = app.test_client().post("/api/upload", data={
        "device_id": "esp-1", "request_id": "q1", "transcript": "apa yang ada di depan saya"})
    assert response.status_code == 200, response.json
    assert response.json["result"] == "Saya mendeteksi kursi di depan Anda."
    assert response.json["audio_output"] == "esp32"
    camera.capture.assert_called_once()
    assert device.of_type("tts_ready")[0]["request_id"] == "q1"


def test_upload_without_camera_still_answers_gracefully(system):
    app, ctx, device, camera, *_ = system
    camera.capture.return_value = None
    response = app.test_client().post("/api/upload", data={
        "device_id": "esp-1", "request_id": "q2", "transcript": "apa yang ada di depan saya"})
    assert response.status_code == 200 and "snapshot" in response.json["result"]


def test_web_ui_is_not_served_by_default(system):
    app, *_ = system
    assert app.test_client().get("/").status_code == 404
    assert app.test_client().get("/app.js").status_code == 404
