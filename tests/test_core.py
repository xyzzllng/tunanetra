import io
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from flask import Flask

from ai.pipeline import VisionPipeline
from ai.runtime import LazyService
from ai.spatial import SpatialAnalyzer, consensus
from services.quick_commands import register_quick_commands
from services.sessions import SessionManager


def box(label="chair", confidence=.8, rect=None):
    return {"label": label, "confidence": confidence, "box": rect or [200, 200, 200, 200], "area_ratio": .13}


@pytest.fixture
def frame():
    return np.full((480, 640, 3), 128, np.uint8)


@pytest.fixture
def services():
    return {"object": Mock(detect_boxes=Mock(return_value=[box()]), diagnostics=Mock(return_value={})),
            "expression": Mock(detect_faces=Mock(return_value=[]), summarize=Mock(return_value="Tidak ada wajah")),
            "sign": Mock(analyze=Mock(return_value={"text": "Terdeteksi: Dilarang Parkir.", "confidence": .9,
                                                   "reliable": True, "labels": ["Dilarang Parkir"],
                                                   "boxes": [{"box": [10, 10, 50, 50], "label": "Dilarang Parkir",
                                                              "confidence": .9}]})),
            "scene": Mock(detect_boxes=Mock(return_value=[]))}


def test_consensus_rejects_single_frame_false_positive():
    assert consensus([[], [], [box()]]) == []


def test_consensus_no_stale_ghosts():
    assert consensus([[box()], [box()], []]) == []


def test_consensus_median_not_inflated():
    result = consensus([[box(confidence=.6)], [box(confidence=.7)], [box(confidence=.8)]])[0]
    assert result["confidence"] == .7
    assert result["temporal_hits"] == 3


def test_consensus_one_to_one():
    assert len(consensus([[box()], [box(), box(confidence=.7)]])) == 1


def test_single_frame_marked_unverified():
    assert consensus([[box()]])[0]["temporal_verified"] is False


def test_current_coordinates_retained():
    result = consensus([[box(rect=[202, 200, 200, 200])], [box()]])[0]
    assert result["box"] == [200, 200, 200, 200]


def test_prioritize_path_not_small_side_object():
    spatial = SpatialAnalyzer()
    boxes = spatial.annotate([box("bottle", .99, [10, 20, 30, 50]), box()], 640, 480)
    assert boxes[0]["label"] == "chair"
    assert boxes[0]["possible_obstacle"]
    assert not boxes[1]["possible_obstacle"]
    assert boxes[0]["distance_cm"] is None


def test_distance_50cm_scaling_and_clipping(monkeypatch, tmp_path):
    calibration = tmp_path/"calibration.json"
    calibration.write_text(json.dumps({"camera_id": "server", "label": "bottle", "focal_px": 800,
                                      "image_width": 1280, "image_height": 960, "real_width_cm": 10}))
    monkeypatch.setenv("DISTANCE_CALIBRATION_FILE", str(calibration))
    analyzer = SpatialAnalyzer()
    item = box("bottle", rect=[100, 100, 80, 100])
    assert analyzer.distance(item, 640, 480, "server") == 50
    assert analyzer.distance(item, 640, 480, "other") is None
    assert analyzer.distance(item, 640, 360, "server") is None
    assert analyzer.distance(box("bottle", rect=[0, 0, 80, 80]), 640, 480, "server") is None
    assert analyzer.distance(box("person"), 640, 480, "server") is None


def test_invalid_calibration_graceful(monkeypatch, tmp_path):
    monkeypatch.setenv("DISTANCE_CALIBRATION_FILE", str(tmp_path/"absent.json"))
    spatial = SpatialAnalyzer()
    assert spatial.calibration is None and spatial.calibration_error


@pytest.mark.parametrize("mode,service", [("objek", "object"), ("ekspresi", "expression"), ("rambu", "sign")])
def test_only_requested_mode(mode, service, services, frame):
    pipe = VisionPipeline(services)
    result = pipe.run(mode, [frame]*3 if mode == "objek" else [frame])
    assert result["mode"] == mode
    for name, other in services.items():
        if name != service and not (mode == "objek" and name == "scene"):
            assert other.mock_calls == []


def test_scene_error_does_not_erase_yolo(services, frame):
    services["scene"].detect_boxes.side_effect = RuntimeError("missing")
    result = VisionPipeline(services).run("objek", [frame]*3)
    assert result["detected_objects"] and "scene" in result["errors"]
    assert "tidak aktif" in result["result"]


def test_yolo_error_still_returns_sensor_warning(services, frame):
    services["object"].detect_boxes.side_effect = RuntimeError("missing")
    result = VisionPipeline(services).run("objek", [frame]*3, sensor={"distance_cm": 40})
    assert "40 cm" in result["result"] and "object" in result["errors"]


def test_empty_objects_never_means_safe(services, frame):
    services["object"].detect_boxes.return_value = []
    result = VisionPipeline(services).run("objek", [frame]*3)
    assert "tidak memastikan jalur aman" in result["result"]


def test_bad_frames_rejected(services, frame):
    pipeline = VisionPipeline(services)
    for frames in ([], [None], [frame, frame[:200]]):
        with pytest.raises(ValueError):
            pipeline.run("objek", frames)


def test_sign_error_not_empty_success(services, frame):
    services["sign"].analyze.return_value = {"error": "missing weights"}
    with pytest.raises(RuntimeError):
        VisionPipeline(services).run("rambu", [frame])


def test_sign_result_fields_and_spoken_text(services, frame):
    result = VisionPipeline(services).run("rambu", [frame])
    assert result["sign_text"] == "Terdeteksi: Dilarang Parkir." and result["sign_reliable"]
    assert result["sign_boxes"][0]["label"] == "Dilarang Parkir"
    assert result["result"] == "Terdeteksi: Dilarang Parkir."


def test_unreliable_sign_never_mentions_text_reading(services, frame):
    services["sign"].analyze.return_value = {"text": "", "confidence": 0.0, "reliable": False,
                                              "labels": [], "boxes": []}
    result = VisionPipeline(services).run("rambu", [frame])
    assert "Rambu lalu lintas belum terdeteksi jelas" in result["result"]
    assert "ulisan" not in result["result"]


def test_old_text_mode_is_gone(services, frame):
    with pytest.raises(ValueError):
        VisionPipeline(services).run("baca", [frame])


def test_session_claim_and_repairing():
    sessions = SessionManager()
    sessions.create_session("a")
    sessions.create_session("b")
    assert sessions.begin_request("a", "1")[0] == "new"
    assert sessions.begin_request("a", "2")[0] == "in_progress"
    sessions.fail_request("a", "2")
    assert sessions.begin_request("a", "2")[0] == "in_progress"
    sessions.finish_request("a", "1", "", {"status": "success"})
    assert sessions.begin_request("a", "1")[0] == "cached"
    sessions.link_device("dev", "a")
    sessions.record_distance("a", 40)
    sessions.link_device("dev", "b")
    assert sessions.device_for_session("a") is None
    assert "distance_samples" not in sessions.get("a")
    assert sessions.session_for_device("dev") == "b"


def test_lazy_import_error_is_retry_limited():
    svc = LazyService("missing_module_intentionally", "Missing")
    assert svc.status()["state"] == "not_loaded"
    with pytest.raises(RuntimeError):
        svc.get()
    retry = svc.retry_at
    with pytest.raises(RuntimeError):
        svc.get()
    assert svc.retry_at == retry and svc.status()["state"] == "error"


@pytest.fixture
def api(services):
    app = Flask(__name__)
    app.testing = True
    sessions = SessionManager()
    register_quick_commands(app, services, sessions, lambda: None, lambda: None)
    return app, sessions


def payload(frame, mode="objek", rid="r1", sid="s1"):
    raw = cv2.imencode(".jpg", frame)[1].tobytes()
    return {"session_id": sid, "request_id": rid, "mode": mode,
            "image": [(io.BytesIO(raw), "frame.jpg") for _ in range(3 if mode == "objek" else 1)]}


def test_quick_api_result_and_dedup(api, frame, services):
    app, _ = api
    client = app.test_client()
    first = client.post("/api/quick", data=payload(frame))
    assert first.status_code == 200 and first.json["detected_objects"]
    again = client.post("/api/quick", data=payload(frame))
    assert again.json["request_id"] == "r1"
    assert services["object"].detect_boxes.call_count == 3


def test_quick_no_prompt_dedupe_for_new_click(api, frame, services):
    app, _ = api
    client = app.test_client()
    client.post("/api/quick", data=payload(frame))
    client.post("/api/quick", data=payload(frame, rid="r2"))
    assert services["object"].detect_boxes.call_count == 6


def test_stale_sensor_not_used(api, frame):
    app, sessions = api
    sessions.create_session("s1")
    sessions.update("s1", distance_samples=[(time.time()-10, 45)])
    result = app.test_client().post("/api/quick", data=payload(frame)).json
    assert result["sensor_distance"] is None


def test_sensor_is_not_assigned_to_visual_object(api, frame):
    app, sessions = api
    sessions.create_session("s1")
    sessions.record_distance("s1", 40)
    result = app.test_client().post("/api/quick", data=payload(frame)).json
    assert result["sensor_distance"]["distance_cm"] == 40
    assert result["detected_objects"][0]["distance_cm"] is None


@pytest.mark.parametrize("data", [{}, {"session_id": "../bad", "request_id": "r1", "mode": "objek"},
    {"session_id": "s1", "request_id": "r1", "mode": "nope"},
    {"session_id": "s1", "request_id": "r1", "mode": "objek"}])
def test_bad_requests(api, data):
    app, _ = api
    assert app.test_client().post("/api/quick", data=data).status_code == 400


def test_invalid_jpeg_and_retry(api, frame):
    app, _ = api
    client = app.test_client()
    bad = {"session_id": "s1", "request_id": "r1", "mode": "objek", "image": (io.BytesIO(b"bad"), "a.jpg")}
    assert client.post("/api/quick", data=bad).status_code == 400
    assert client.post("/api/quick", data=payload(frame)).status_code == 200


def test_mode_error_does_not_lock_next_request(api, frame, services):
    app, _ = api
    services["expression"].detect_faces.side_effect = RuntimeError("broken model")
    client = app.test_client()
    assert client.post("/api/quick", data=payload(frame, mode="ekspresi")).status_code == 503
    assert client.post("/api/quick", data=payload(frame, mode="rambu", rid="r2")).status_code == 200


def test_local_mode_does_not_require_browser_camera(services, frame):
    camera = Mock(capture_frames=Mock(return_value=[frame]*3))
    app = Flask(__name__)
    register_quick_commands(app, services, SessionManager(), lambda: None, lambda: None, camera=camera)
    client = app.test_client()
    result = client.post("/api/quick", data={"session_id": "s1", "request_id": "r1", "mode": "objek"})
    assert result.status_code == 200
    camera.capture_frames.assert_called_once_with(3)


def test_concurrent_sessions_backpressure(api, frame, services):
    app, _ = api
    entered, release = threading.Event(), threading.Event()
    def block(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return [box()]
    services["object"].detect_boxes.side_effect = block
    thread = threading.Thread(target=lambda: app.test_client().post("/api/quick", data=payload(frame)))
    thread.start()
    assert entered.wait(3)
    try:
        second = app.test_client().post("/api/quick", data=payload(frame, sid="s2"))
        assert second.status_code == 429
    finally:
        release.set()
        thread.join(3)


def test_sensor_expiring_during_inference_is_omitted(api, frame, services):
    app, sessions = api
    sessions.create_session("s1")
    sessions.record_distance("s1", 40)
    def expire(*args, **kwargs):
        sessions.update("s1", distance_samples=[(time.time()-10, 40)])
        return [box()]
    services["object"].detect_boxes.side_effect = expire
    result = app.test_client().post("/api/quick", data=payload(frame)).json
    assert result["sensor_distance"] is None
    assert "40 cm" not in result["result"]


def test_audio_and_database_job_does_not_block_visual_result(services, frame, tmp_path, monkeypatch):
    import services.quick_commands as quick_module
    monkeypatch.setattr(quick_module, "BASE_DIR", tmp_path)
    entered, release = threading.Event(), threading.Event()
    def tts(_text):
        entered.set()
        assert release.wait(3)
        return io.BytesIO(b"fake-audio")
    audio = Mock(generate_tts=Mock(side_effect=tts))
    database = Mock()
    app = Flask(__name__)
    sessions = SessionManager()
    push = Mock()
    sessions.create_session("s1")
    sessions.link_device("esp-test", "s1")
    sessions.register_esp32_socket("esp-test", object())
    register_quick_commands(app, services, sessions, lambda: audio, lambda: database, push_device=push)
    client = app.test_client()
    response = client.post("/api/quick", data=payload(frame))
    assert response.status_code == 200
    assert entered.wait(1)
    assert client.get(response.json["audio_job"]).json["status"] == "pending"
    release.set()
    for _ in range(100):
        state = client.get(response.json["audio_job"]).json
        if state["status"] == "ready" and database.save_interaction.called:
            break
        time.sleep(.01)
    assert state["audio_output"] == "esp32"
    push.assert_called_once()
    database.save_interaction.assert_called_once()
    assert client.get(state["audio_url"]).status_code == 200
