"""Hub event tombol fisik ESP32 dan sesi perangkat (tanpa AI, tanpa jaringan)."""
import pytest
from flask import Flask

from config.settings import settings
from devices.events import ESP32EventHub, normalize_event, register_esp32_events
from services.sessions import SessionManager


def make_hub(**kwargs):
    calls = []

    def trigger(**kw):
        calls.append(kw)
        return {"status": "started", "request_id": f"r{len(calls)}"}
    kwargs.setdefault("cooldown_ms", 0)
    kwargs.setdefault("trigger", trigger)
    return ESP32EventHub(SessionManager(), **kwargs), calls


@pytest.mark.parametrize("button,mode", [(1, "objek"), (2, "ekspresi"), (3, "rambu")])
def test_each_button_triggers_its_mode_on_a_device_session(button, mode):
    hub, calls = make_hub()
    monitor = hub.subscribe("monitor")
    outcome = hub.dispatch(button=button, device_id="device-a", source="websocket")
    assert outcome["delivered"] == 1 and outcome["status"] == "started"
    assert calls[-1]["mode"] == mode and calls[-1]["session_id"] == "dev-device-a"
    assert monitor.queue.get_nowait()["type"] == "quick_started"


def test_event_names_and_aliases_map_to_modes():
    assert normalize_event("object_detection")[1] == "objek"
    assert normalize_event("Expression-Detection")[1] == "ekspresi"
    assert normalize_event("sign_detection")[1] == "rambu"
    assert normalize_event(button=3) == ("sign_detection", "rambu")
    # nama lama dari firmware/klien lama tetap diterima, tapi memicu deteksi rambu
    assert normalize_event("text_detection") == ("sign_detection", "rambu")
    assert normalize_event("baca")[1] == "rambu"
    with pytest.raises(ValueError):
        normalize_event("ocr")
    for bad in ({"event": "hack"}, {"button": 9}, {"button": "x"}, {}):
        with pytest.raises(ValueError):
            normalize_event(**bad)


def test_cooldown_ignores_duplicate_press_but_not_other_buttons():
    hub, calls = make_hub(cooldown_ms=60000)
    assert hub.dispatch(button=1, device_id="d")["delivered"] == 1
    again = hub.dispatch(button=1, device_id="d")
    assert again["status"] == "ignored" and again["delivered"] == 0
    assert hub.dispatch(button=2, device_id="d")["delivered"] == 1
    assert len(calls) == 2


def test_rejected_trigger_is_reported_and_not_delivered():
    hub, _ = make_hub(trigger=lambda **kw: {"status": "busy", "reason": "sibuk"})
    monitor = hub.subscribe("m")
    outcome = hub.dispatch(button=1, device_id="d")
    assert outcome["status"] == "busy" and outcome["delivered"] == 0
    assert monitor.queue.get_nowait()["type"] == "quick_rejected"


def test_missing_or_crashing_trigger_never_raises():
    hub, _ = make_hub(trigger=None)
    assert hub.dispatch(button=1, device_id="d")["status"] == "no_trigger"
    hub2, _ = make_hub(trigger=lambda **kw: 1 / 0)
    assert hub2.dispatch(button=1, device_id="d")["status"] == "error"


def test_existing_pairing_is_kept_over_the_default_session():
    hub, calls = make_hub()
    hub._sessions.create_session("phone")
    hub._sessions.link_device("d", "phone")
    hub.dispatch(button=1, device_id="d")
    assert calls[-1]["session_id"] == "phone"


def test_slow_monitor_queue_drops_oldest_and_never_blocks():
    hub, _ = make_hub()
    monitor = hub.subscribe("slow")
    for _ in range(100):
        hub.dispatch(button=1, device_id="d")
    assert monitor.queue.qsize() == 32


def test_rest_endpoint_requires_token_when_configured():
    app = Flask(__name__)
    hub = register_esp32_events(app, SessionManager(), settings=type("S", (), {
        "esp32_event_cooldown_ms": 0, "esp32_shared_token": "rahasia",
        "esp32_max_stream_clients": 4, "esp32_sse_heartbeat_seconds": 15})())
    hub.trigger = lambda **kw: {"status": "started", "request_id": "x"}
    client = app.test_client()
    assert client.post("/api/esp32/event", json={"button": 1}).status_code == 401
    ok = client.post("/api/esp32/event", json={"button": 1, "token": "rahasia"})
    assert ok.status_code == 200 and ok.json["status"] == "started"
    assert client.post("/api/esp32/event", json={"button": 8, "token": "rahasia"}).status_code == 400
    assert client.get("/api/esp32/status").json["trigger_ready"] is True


def test_ensure_device_session_is_stable_and_sanitised():
    sessions = SessionManager()
    first = sessions.ensure_device_session("esp32/A 1")
    assert first == "dev-esp32_A_1" and sessions.ensure_device_session("esp32/A 1") == first
    assert sessions.device_for_session(first) == "esp32/A 1"
    assert sessions.ensure_device_session("") == "dev-unknown"


def test_status_reports_connected_devices_and_only_fresh_distance_samples():
    sessions = SessionManager()
    session_id = sessions.ensure_device_session("main-01")
    sessions.register_esp32_socket("main-01", object())
    sessions.record_distance(session_id, 63.2)

    hub = ESP32EventHub(sessions, trigger=lambda **kwargs: {"status": "started"})
    status = hub.status()

    assert status["connected_devices"] == ["main-01"]
    assert status["distance"]["distance_cm"] == 63.2
    assert status["distance"]["device_id"] == "main-01"

    sessions.unregister_esp32_socket("main-01")
    assert hub.status()["connected_devices"] == []


def test_old_distance_samples_are_not_reported_as_current_telemetry():
    sessions = SessionManager()
    session_id = sessions.ensure_device_session("main-01")
    sessions.record_distance(session_id, 42.0)
    state = sessions.get(session_id)
    state["distance_samples"][0] = (state["distance_samples"][0][0] - 5, 42.0)
    sessions.update(session_id, distance_samples=state["distance_samples"])

    assert sessions.latest_distance() is None
