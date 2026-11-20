"""Quick Command: satu tugas visi (objek / ekspresi / rambu) per perintah.

Dua pintu masuk, satu logika (``QuickCommandService.run``):

* **Tombol fisik ESP32** -> ``devices.events`` -> ``start_from_button()``.
  Backend memotret sendiri lewat ESP32-CAM, menjalankan AI, lalu mengirim
  suara ke speaker ESP32 (``tts_ready``). Tidak ada browser/webcam.
* **HTTP** ``POST /api/quick`` (uji manual, monitor opsional). Snapshot boleh
  dikirim klien; bila tidak ada, backend memotret lewat ESP32-CAM.

Satu pekerjaan visi aktif per proses; permintaan bersamaan ditolak (bukan
diantrekan tanpa batas). TTS dan pencatatan database berjalan di antrean
latar terbatas sehingga hasil visual tidak tertunda.
"""
import base64
import io
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from flask import Blueprint, Response, jsonify, request, send_from_directory
from PIL import Image

from ai.pipeline import VisionPipeline
from camera import CameraError
from config.settings import BASE_DIR
from services.esp32_audio_push import push_mp3_to_esp32

MODE_LABEL = {
    "objek": "Deteksi Objek",
    "ekspresi": "Deteksi Ekspresi",
    "rambu": "Deteksi Rambu Lalu Lintas",
}
CAMERA_DOWN_SPOKEN = "Kamera tidak terhubung. Periksa daya dan Wi-Fi kamera."

_DETECTION_GALLERY_HTML = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<title>Spider Sense — Galeri Deteksi</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 0; padding: 16px; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  #status { color: #999; font-size: 13px; margin-bottom: 12px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
  .card { background: #1c1c1c; border-radius: 8px; overflow: hidden; }
  .card img { width: 100%; display: block; }
  .card .name { font-size: 11px; color: #aaa; padding: 6px 8px; word-break: break-all; }
  .empty { color: #888; padding: 24px 0; }
</style>
</head>
<body>
<h1>Spider Sense — Galeri Hasil Deteksi</h1>
<div id="status">Memuat...</div>
<div class="grid" id="grid"></div>
<script>
async function load() {
  const res = await fetch('/api/detection_images?limit=60');
  const data = await res.json();
  const status = document.getElementById('status');
  const grid = document.getElementById('grid');
  if (data.status === 'disabled') {
    status.textContent = data.message;
    grid.innerHTML = '';
    return;
  }
  status.textContent = data.count + ' gambar tersimpan (terbaru di atas). Auto-refresh tiap 5 detik.';
  grid.innerHTML = data.items.map(function(item) {
    return '<div class="card"><img src="' + item.url + '" loading="lazy">' +
           '<div class="name">' + item.name + '</div></div>';
  }).join('') || '<div class="empty">Belum ada gambar. Coba tombol Quick Command dulu.</div>';
}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


class QuickCommandService:
    def __init__(self, services, sessions, audio_getter, database_getter, camera=None,
                 push_device=None, notify=None, logger=None):
        self.sessions = sessions
        self.audio_getter = audio_getter
        self.database_getter = database_getter
        self.camera = camera
        self.push_device = push_device
        self.notify = notify or (lambda payload: None)
        self.log = logger or logging.getLogger(__name__)
        self.pipeline = VisionPipeline(services)
        self.services = services

        self._vision_lock = threading.Lock()
        self._button_gate = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._audio_slots = threading.BoundedSemaphore(8)
        self._output_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quick-output")
        self._button_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quick-button")
        self.jobs = {}
        self.audio_dir = BASE_DIR / "audio_storage"
        self.audio_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------- sensor
    def fresh_sensor(self, sid):
        state = self.sessions.get(sid) or {}
        values = [(t, d) for t, d in state.get("distance_samples", []) if 0 <= time.time() - t <= 4.0]
        if not values:
            return None
        # Jangan menghaluskan halangan yang tiba-tiba lebih dekat.
        distance = min(values[-1][1], sorted(d for _, d in values)[len(values) // 2])
        return {"distance_cm": distance, "source": "ultrasonic",
                "age_ms": int((time.time() - values[-1][0]) * 1000), "associated_object": None}

    # --------------------------------------------------------------- audio
    def _cleanup_jobs(self):
        cutoff = time.monotonic() - 300
        with self._jobs_lock:
            for key in [k for k, v in self.jobs.items() if v["created"] < cutoff and v["status"] != "pending"]:
                self.jobs.pop(key, None)
        # Hanya berkas keluaran API ini, setelah satu jam masa putar ulang.
        for path in self.audio_dir.glob("quick_*.mp3"):
            try:
                if time.time() - path.stat().st_mtime > 3600:
                    path.unlink()
            except OSError:
                pass

    def _device_online(self, sid):
        device = self.sessions.device_for_session(sid)
        online = bool(device and self.push_device and self.sessions.get_esp32_socket(device))
        return device, online

    def _output_worker(self, sid, rid, data, from_camera):
        key = (sid, rid)
        try:
            audio = self.audio_getter()
            if audio is None:
                raise RuntimeError("TTS tidak tersedia")
            name = f"quick_{sid}_{rid}.mp3"
            mp3_bytes = audio.generate_tts(data["result"]).getvalue()
            (self.audio_dir / name).write_bytes(mp3_bytes)
            device, online = self._device_online(sid)
            url = "/api/quick_audio_file/" + name
            # Perintah yang lebih baru membatalkan audio dari TTS lambat sebelumnya.
            if (self.sessions.get(sid) or {}).get("quick_latest_request") != rid:
                state = {"status": "superseded"}
            elif online:
                esp32_ip = self.sessions.get_device_ip(device)
                self.log.info(
                    "[quick_commands] tombol -> push audio: device=%s esp32_ip=%s "
                    "mp3_bytes=%d sid=%s rid=%s", device, esp32_ip, len(mp3_bytes), sid, rid)
                pushed = bool(esp32_ip) and push_mp3_to_esp32(mp3_bytes, esp32_ip)
                if not pushed:
                    self.log.warning(
                        "[quick_commands] push audio GAGAL, fallback ke speaker laptop: "
                        "device=%s esp32_ip=%s sid=%s rid=%s", device, esp32_ip, sid, rid)
                if pushed:
                    state = {"status": "ready", "audio_url": url, "audio_output": "esp32"}
                elif from_camera:
                    played = bool(audio._play_audio(str(self.audio_dir / name)))
                    state = {"status": "ready", "audio_url": url,
                             "audio_output": "local" if played else "none"}
                else:
                    state = {"status": "ready", "audio_url": url, "audio_output": "browser"}
            elif from_camera:
                played = bool(audio._play_audio(str(self.audio_dir / name)))
                state = {"status": "ready", "audio_url": url, "audio_output": "local" if played else "none"}
            else:
                state = {"status": "ready", "audio_url": url, "audio_output": "browser"}
        except Exception:
            state = {"status": "error", "message": "TTS tidak tersedia; gunakan suara browser."}
        finally:
            with self._jobs_lock:
                if key in self.jobs:
                    self.jobs[key].update(state)
        # Kegagalan database tidak boleh membatalkan hasil visual yang sukses.
        try:
            database = self.database_getter()
            if database:
                database.save_interaction({
                    "session_id": sid, "request_id": rid, "mode": data["mode"],
                    "prompt": f"Quick Command: {data['mode']}", "response": data["result"],
                    "detected_objects": data["detected_objects"], "sign_text": data["sign_text"],
                    "faces": data["faces"], "sign_confidence": data["sign_confidence"]})
        except Exception:
            self.log.exception("Gagal menyimpan hasil quick command")
        finally:
            self._audio_slots.release()

    def announce(self, sid, text, *, voice=True):
        """Umumkan kegagalan ke speaker ESP32 (suara bila bisa, beep rendah bila tidak).

        Pengguna tunanetra tidak boleh mengalami kegagalan yang senyap.
        """
        device, online = self._device_online(sid)
        if not online:
            return False
        if voice:
            try:
                audio = self.audio_getter()
                if audio is not None:
                    name = f"quick_{sid}_err{uuid.uuid4().hex[:8]}.mp3"
                    mp3_bytes = audio.generate_tts(text).getvalue()
                    (self.audio_dir / name).write_bytes(mp3_bytes)
                    esp32_ip = self.sessions.get_device_ip(device)
                    if esp32_ip and push_mp3_to_esp32(mp3_bytes, esp32_ip):
                        return True
            except Exception:
                self.log.warning("TTS pengumuman gagal; memakai beep", exc_info=True)
        self.push_device(device, {"type": "error", "message": text})
        return True

    # ---------------------------------------------------------------- core
    def run(self, sid, rid, mode, *, frames=None, camera_id=""):
        """Jalankan satu mode. Kembalikan ``(payload, http_status)``.

        ``frames=None`` berarti memotret lewat kamera server (ESP32-CAM).
        """
        self.sessions.create_session(sid)
        status, cached = self.sessions.begin_request(sid, rid, prompt_key="", repeat_window_seconds=0)
        if status == "cached":
            return dict(cached), 200
        if status == "in_progress":
            return {"status": "busy", "message": "Permintaan sebelumnya masih berjalan"}, 409
        if not self._vision_lock.acquire(blocking=False):
            self.sessions.fail_request(sid, rid)
            return {"status": "busy", "message": "AI sedang dipakai. Coba kembali sebentar lagi."}, 429
        try:
            expected = self.pipeline.frame_count(mode)
            self.sessions.update(sid, quick_latest_request=rid)
            from_camera = frames is None and self.camera is not None
            if from_camera:
                frames = self.camera.capture_frames(expected)
            elif frames is None or not 1 <= len(frames) <= expected:
                raise ValueError(f"Kirim 1 sampai {expected} snapshot untuk mode ini")
            received_at = time.time()
            result = self.pipeline.run(mode, frames, camera_id=camera_id or ("esp32cam" if from_camera else ""))
            # Inferensi bisa makan detik: cek kesegaran sensor saat selesai.
            if mode == "objek":
                sensor = self.fresh_sensor(sid)
                result["sensor_distance"] = sensor
                result["result"] = self.pipeline.spatial.summarize(result["detected_objects"], sensor)
                if not result.get("scene_available"):
                    result["result"] += " Pengenalan tembok dan pintu tidak aktif."
            result["status"] = "partial" if result.get("errors") else "success"
            result.update(request_id=rid, received_at=received_at, completed_at=time.time())
            if from_camera:
                ok, encoded = cv2.imencode(".jpg", frames[-1], [cv2.IMWRITE_JPEG_QUALITY, 88])
                if ok:
                    result["image_url"] = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
            self.sessions.update(
                sid, mode=mode, last_result=result["result"], last_objects=result["detected_objects"],
                last_faces=result["faces"], last_signs=result["sign_boxes"], last_sign_text=result["sign_text"],
                last_sign_confidence=result["sign_confidence"], last_sign_reliable=result["sign_reliable"],
                last_vision_at=time.time())
            self._cleanup_jobs()
            if self._audio_slots.acquire(blocking=False):
                with self._jobs_lock:
                    self.jobs[(sid, rid)] = {"status": "pending", "created": time.monotonic()}
                self._output_pool.submit(self._output_worker, sid, rid, dict(result), from_camera)
                result["audio_job"] = f"/api/quick_audio/{sid}/{rid}"
            else:
                result["warnings"].append("Antrean audio penuh; gunakan suara browser. Pencatatan hasil ini dilewati.")
            self.sessions.finish_request(sid, rid, "", result)
            return result, 200
        except ValueError as exc:
            self.sessions.fail_request(sid, rid)
            return {"status": "error", "message": str(exc)}, 400
        except CameraError as exc:
            self.sessions.fail_request(sid, rid)
            self.log.warning("Kamera gagal: %s", exc)
            return {"status": "error", "message": str(exc), "spoken": CAMERA_DOWN_SPOKEN}, 503
        except Exception as exc:
            self.sessions.fail_request(sid, rid)
            self.log.exception("Quick command gagal")
            return {"status": "error", "message": f"Mode {mode} belum tersedia: {exc}",
                    "spoken": f"{MODE_LABEL.get(mode, mode)} belum tersedia."}, 503
        finally:
            self._vision_lock.release()

    # ------------------------------------------------------- tombol fisik
    def start_from_button(self, *, mode, device_id, session_id, label="", source="button"):
        """Mulai satu perintah dari tombol fisik tanpa memblokir thread WebSocket."""
        if not self._button_gate.acquire(blocking=False):
            return {"status": "busy", "reason": "perintah sebelumnya masih berjalan"}
        rid = f"btn-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
        try:
            self._button_pool.submit(self._button_job, rid, session_id, mode, device_id, label)
        except Exception:
            self._button_gate.release()
            raise
        return {"status": "started", "request_id": rid, "session_id": session_id}

    def _button_job(self, rid, sid, mode, device_id, label):
        payload, code = {}, 500
        try:
            payload, code = self.run(sid, rid, mode)
            if code != 200:
                if code in (409, 429):
                    self.announce(sid, payload.get("message", ""), voice=False)
                else:
                    self.announce(sid, payload.get("spoken") or f"{label or mode} gagal.")
        except Exception:
            self.log.exception("Perintah tombol gagal")
            try:
                self.announce(sid, "Terjadi kesalahan pada sistem.")
            except Exception:
                pass
        finally:
            self._button_gate.release()
            try:
                self.notify({"type": "quick_result", "mode": mode, "label": label, "device_id": device_id,
                             "request_id": rid, "http_status": code,
                             "result": payload.get("result") or payload.get("message", ""),
                             "processing_ms": payload.get("processing_ms")})
            except Exception:
                pass


def register_quick_commands(app, services, sessions, audio_getter, database_getter,
                            camera=None, push_device=None, notify=None):
    """Pasang endpoint ``/api/quick*`` dan kembalikan ``QuickCommandService``."""
    service = QuickCommandService(services, sessions, audio_getter, database_getter,
                                  camera=camera, push_device=push_device, notify=notify,
                                  logger=app.logger)
    api = Blueprint("quick", __name__)
    max_pixels = 6_000_000
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

    def decode(upload):
        raw = upload.read()
        try:
            with Image.open(io.BytesIO(raw)) as im:
                if im.width * im.height > max_pixels or min(im.size) < 32:
                    raise ValueError("Ukuran snapshot harus 32 piksel atau lebih dan maksimal 6 megapiksel")
                if max(im.size) / min(im.size) > 8:
                    raise ValueError("Rasio snapshot tidak didukung; gunakan rasio kamera biasa")
                if im.format not in {"JPEG", "PNG"}:
                    raise ValueError("Snapshot harus JPEG/PNG")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("Snapshot tidak valid") from exc
        frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("Snapshot tidak dapat dibaca")
        return frame

    @api.get("/api/client_config")
    def client_config():
        return jsonify(
            camera_source="server",         # kamera selalu milik backend (ESP32-CAM), bukan browser
            object_frames=service.pipeline.burst_count, expression_frames=service.pipeline.expression_frames,
            burst_gap_ms=100, max_snapshot_width=1920,
            services={name: svc.status() for name, svc in services.items()})

    @api.post("/api/quick")
    def quick():
        sid = request.form.get("session_id", "")
        rid = request.form.get("request_id", "")
        mode = request.form.get("mode", "")
        if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", v) for v in (sid, rid)):
            return jsonify(status="error", message="session_id/request_id tidak valid"), 400
        if mode not in MODE_LABEL:
            return jsonify(status="error", message="Mode tidak valid"), 400
        try:
            files = request.files.getlist("image")
            frames = [decode(f) for f in files] if files else None
        except ValueError as exc:
            return jsonify(status="error", message=str(exc)), 400
        payload, code = service.run(sid, rid, mode, frames=frames,
                                    camera_id=request.form.get("camera_id", ""))
        return jsonify(payload), code

    @api.get("/api/quick_audio/<sid>/<rid>")
    def audio_status(sid, rid):
        with service._jobs_lock:
            value = dict(service.jobs.get((sid, rid), {"status": "expired"}))
        value.pop("created", None)
        return jsonify(value)

    @api.get("/api/quick_audio_file/<name>")
    def audio_file(name):
        if not re.fullmatch(r"quick_[A-Za-z0-9_-]+\.mp3", name):
            return jsonify(status="error"), 404
        return send_from_directory(service.audio_dir, name, mimetype="audio/mpeg", conditional=False)

    # -------------------------------------------------- galeri hasil deteksi
    # Hanya aktif bila SAVE_DETECTION_IMAGES=true di .env. Untuk dilihat
    # manusia lewat browser saja; tidak dipakai alur AI/TTS mana pun.
    @api.get("/api/detection_images")
    def detection_images():
        pipeline = service.pipeline
        if not pipeline.save_detection_images:
            return jsonify(status="disabled",
                            message="Set SAVE_DETECTION_IMAGES=true di .env untuk mengaktifkan."), 200
        directory = pipeline.detection_image_dir
        try:
            names = sorted(
                (n for n in os.listdir(directory) if n.endswith(".jpg")),
                key=lambda n: os.path.getmtime(os.path.join(directory, n)),
                reverse=True,
            )
        except OSError:
            names = []
        limit = min(200, max(1, int(request.args.get("limit", 50))))
        items = [{"name": n, "url": f"/api/detection_image_file/{n}"} for n in names[:limit]]
        return jsonify(status="success", count=len(names), items=items)

    @api.get("/api/detection_image_file/<name>")
    def detection_image_file(name):
        pipeline = service.pipeline
        if not re.fullmatch(r"(objek|ekspresi)_[0-9_]+\.jpg", name):
            return jsonify(status="error"), 404
        return send_from_directory(pipeline.detection_image_dir, name, mimetype="image/jpeg", conditional=False)

    @api.get("/detections")
    def detection_gallery():
        """Halaman HTML sederhana untuk melihat galeri hasil deteksi di browser."""
        return Response(_DETECTION_GALLERY_HTML, mimetype="text/html")

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(status="error", message="Upload terlalu besar; maksimum 16 MiB per perintah."), 413

    app.register_blueprint(api)
    app.extensions["quick_service"] = service
    return service