"""Tanya-jawab suara/teks (Groq) dan penyaji berkas MP3.

``POST /api/upload`` menerima audio (Whisper) atau ``transcript`` teks, memilih
mode (percakapan, deskripsi objek, ekspresi, hitung wajah, rambu, tindak lanjut),
lalu menjawab lewat TTS. Bagian visi memakai snapshot kiriman klien bila ada;
bila tidak, backend memotret sendiri lewat ESP32-CAM.

Catatan: mikrofon tidak ada pada firmware ESP32 saat ini, jadi audio hanya
datang dari klien yang bisa merekam (mis. halaman web opsional). ``transcript``
teks bekerja tanpa perangkat tambahan.
"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
import uuid
from datetime import datetime

import cv2
import numpy as np
from flask import abort, jsonify, request, send_from_directory

from ai.output import spoken_text
from config.settings import BASE_DIR
from devices.ws import safe_token
from services.esp32_audio_push import push_mp3_to_esp32


def register_upload(app, ctx):
    """Pasang ``/api/upload``, ``/api/audio_file``, ``/mp3``, ``/api/session``."""
    log = app.logger
    sessions = ctx.sessions
    camera = ctx.camera
    audio_processor = ctx.audio
    groq_ai = ctx.groq
    database = ctx.database
    object_detector = ctx.vision["object"]
    emotion_detector = ctx.vision["expression"]
    sign_detector = ctx.vision["sign"]
    service_init_errors = ctx.init_errors
    audio_dir = ctx.audio_dir
    PROMPT_REPEAT_GUARD_SECONDS = ctx.settings.prompt_repeat_guard_seconds
    YOLO_DEBUG_SAVE_EMPTY = os.getenv("YOLO_DEBUG_SAVE_EMPTY", "false").lower() in ("1", "true", "yes", "on")
    _safe_filename_token = safe_token

    def _save_tts(text, filename):
        return ctx.link._save_tts(text, filename)

    def push_to_device(device_id, payload):
        return ctx.link.push_to_device(device_id, payload)

    def _prompt_key(transcript, mode):
        normalized = " ".join((transcript or "").lower().split())
        return hashlib.sha1(f"{mode or ''}|{normalized}".encode("utf-8")).hexdigest()

    def _is_simple_scene_question(prompt):
        text = " ".join((prompt or "").lower().split())
        hints = (
            "apa yang ada di depan", "apa di depan", "objek apa", "benda apa",
            "apa yang terlihat", "apa yang ada di sekitar", "lihat apa", "ada apa di depan",
            "ini apa", "itu apa", "benda ini", "barang ini", "yang di depan",
        )
        return any(hint in text for hint in hints)

    @app.route("/api/upload", methods=["POST"])
    def upload():
        session_id = None
        request_id = None
        request_claimed = False
        try:
            session_id = (request.form.get("session_id") or "").strip()
            device_hint = (request.form.get("device_id") or "").strip()
            if not session_id and device_hint:
                # Klien tanpa halaman web (mis. skrip/ESP32) cukup menyebut device_id.
                session_id = sessions.ensure_device_session(device_hint)
            if not session_id:
                return jsonify({"status": "error", "message": "session_id atau device_id wajib disertakan"}), 400
            sessions.create_session(session_id)

            request_id = (request.form.get("request_id") or str(uuid.uuid4())).strip()
            forced_mode = (request.form.get("mode") or "").strip() or None
            transcript = (request.form.get("transcript") or "").strip()
            stt_engine = "client"

            # Preferred voice path: browser/ESP32 sends one recorded audio clip and
            # Groq Whisper turns it into text. The old transcript field remains
            # supported for manual tests and backwards compatibility.
            if not transcript and "audio" in request.files:
                if not audio_processor:
                    return jsonify({"status": "error", "message": "Speech service tidak tersedia"}), 503
                audio_file = request.files["audio"]
                audio_bytes = audio_file.read()
                if len(audio_bytes) < 256:
                    return jsonify({"status": "error", "message": "Data audio terlalu kecil atau kosong"}), 400
                prompt, _saved_audio = audio_processor.process_audio_input(
                    audio_bytes,
                    filename=audio_file.filename or "speech.webm",
                )
                if not prompt or str(prompt).startswith("Error:"):
                    return jsonify({"status": "error", "message": prompt or "Suara tidak dapat dikenali"}), 400
                transcript = prompt.strip()
                stt_engine = "groq-whisper"

            if not transcript and not forced_mode:
                return jsonify({"status": "error", "message": "Tidak ada audio, transcript, atau mode"}), 400

            previous = sessions.get(session_id) or {}
            has_cached_vision = bool(
                previous.get("last_objects")
                or previous.get("last_sign_text")
                or previous.get("last_faces")
            )

            tl = transcript.lower()
            # Compatibility with the original client/ESP32 mode names. Internally
            # object/scene detection is called `deskripsi`, while older clients may
            # still send `objek`. Without this mapping YOLO can be skipped entirely.
            legacy_mode_map = {
                "objek": "deskripsi",
                "wajah": "ekspresi",
                "baca": "rambu",   # nama lama dari klien/firmware lama -> deteksi rambu
                "rambu": "rambu",
                "tanya": "tanya",
            }
            mode = legacy_mode_map.get(forced_mode, forced_mode) if forced_mode else None
            if not mode:
                if "berapa" in tl and ("orang" in tl or "wajah" in tl):
                    mode = "hitung_wajah"
                elif "ekspresi" in tl:
                    mode = "ekspresi"
                elif "wajah" in tl and not (groq_ai and groq_ai.is_visual_followup(transcript) and has_cached_vision):
                    mode = "hitung_wajah"
                elif groq_ai and groq_ai.is_visual_followup(transcript) and has_cached_vision:
                    mode = "followup"
                elif groq_ai and groq_ai.is_sign_question(transcript):
                    mode = "rambu"
                elif any(word in tl for word in ("rambu", "lampu merah", "lampu lalu lintas", "penyebrangan",
                                                  "zebra cross", "dilarang parkir")):
                    mode = "rambu"
                elif groq_ai and groq_ai.is_vision_question(transcript):
                    mode = "deskripsi"
                elif any(word in tl for word in ("objek", "foto", "depan", "lihat", "benda", "sekitar")):
                    mode = "deskripsi"
                else:
                    mode = "tanya"

            prompt_key = _prompt_key(transcript, mode)
            claim, cached = sessions.begin_request(
                session_id,
                request_id,
                prompt_key=prompt_key,
                repeat_window_seconds=PROMPT_REPEAT_GUARD_SECONDS,
            )
            request_claimed = claim not in {"cached", "in_progress"}
            if claim == "cached" and cached:
                cached = dict(cached)
                cached.pop("audio_url", None)
                cached["duplicate_suppressed"] = True
                cached["request_id"] = request_id
                return jsonify(cached)
            if claim == "in_progress":
                return jsonify({
                    "status": "ignored",
                    "message": "Permintaan yang sama sedang diproses.",
                    "duplicate_suppressed": True,
                    "request_id": request_id,
                }), 202

            sessions.update(session_id, last_transcript=transcript)

            # Decode a camera frame only for commands that need a CURRENT visual
            # observation. Conversation and follow-up questions use zero CV work.
            img_data = None
            if mode in {"hitung_wajah", "ekspresi", "rambu", "deskripsi"}:
                if "image" in request.files:
                    file_bytes = request.files["image"].read()
                    npbuf = np.frombuffer(file_bytes, dtype=np.uint8)
                    img_data = cv2.imdecode(npbuf, cv2.IMREAD_COLOR)
                    if img_data is None:
                        sessions.fail_request(session_id, request_id)
                        return jsonify({"status": "error", "message": "Snapshot kamera tidak dapat dibaca"}), 400
                elif camera is not None:
                    # Tanpa snapshot dari klien: potret lewat ESP32-CAM (None bila kamera mati,
                    # lalu jawaban "kamera belum memberikan snapshot" di bawah dipakai).
                    img_data = camera.capture(mode)

            answer = ""
            detected_image = None
            objects = []
            faces = []
            sign_text = ""
            sign_confidence = 0.0
            sign_reliable = False
            sign_boxes = []
            detection_diag = {}

            # ON-DEMAND PIPELINE: only one relevant vision path runs per request.
            if mode == "ekspresi":
                if img_data is None:
                    answer = "Kamera belum memberikan snapshot untuk mendeteksi ekspresi."
                else:
                    faces = emotion_detector.detect_faces(img_data, primary_only=True)
                    answer = emotion_detector.summarize(faces)
                    sessions.update(session_id, last_faces=faces, last_vision_at=time.time())

            elif mode == "hitung_wajah":
                if img_data is None:
                    answer = "Kamera belum memberikan snapshot untuk mendeteksi wajah."
                elif emotion_detector:
                    try:
                        faces = emotion_detector.detect_faces(img_data, primary_only=False, classify=False)
                        sessions.update(session_id, last_faces=faces, last_vision_at=time.time())
                        n = len(faces)
                        answer = f"Ada {n} orang di depan Anda." if n else "Tidak ada orang yang terdeteksi di depan Anda."
                    except Exception as exc:
                        log.warning(f"Deteksi wajah (on-demand) gagal: {exc}")
                        answer = "Deteksi wajah sedang tidak tersedia."
                else:
                    answer = "Deteksi wajah sedang tidak tersedia."

            elif mode == "rambu":
                # Deteksi rambu lalu lintas; tidak menjalankan YOLO deskripsi objek.
                if img_data is None:
                    answer = "Kamera belum memberikan snapshot untuk mendeteksi rambu lalu lintas."
                elif sign_detector:
                    sign = sign_detector.analyze(img_data, dedupe_key=f"{session_id}:question")
                    sign_text = sign.get("text", "")
                    sign_confidence = float(sign.get("confidence", 0.0))
                    sign_reliable = bool(sign.get("reliable", False))
                    sign_boxes = sign.get("boxes", [])
                    detected_image = sign.get("processed_image")
                    sessions.update(
                        session_id,
                        last_signs=sign_boxes,
                        last_sign_text=sign_text,
                        last_sign_confidence=sign_confidence,
                        last_sign_reliable=sign_reliable,
                        last_sign_at=time.time(),
                        last_vision_at=time.time(),
                    )
                    if groq_ai:
                        answer = groq_ai.ask_with_context(
                            transcript or "Apa rambu lalu lintas yang ada di depan?",
                            detected_objects=[],
                            sign_text=sign_text,
                            sign_confidence=sign_confidence,
                            sign_reliable=sign_reliable,
                        )
                    elif sign_text and sign_reliable:
                        answer = spoken_text(sign_text)
                    else:
                        answer = "Rambu/sinyal lalu lintas belum terdeteksi dengan jelas. Arahkan kamera ke rambu atau lampu lalu lintas."
                else:
                    answer = "Deteksi rambu sedang tidak tersedia."

            elif mode == "deskripsi":
                # One snapshot, one YOLO inference. No frame loop and no tracker wait.
                if img_data is None:
                    answer = "Kamera belum memberikan snapshot untuk melihat kondisi di depan."
                else:
                    detector_error = None
                    if object_detector:
                        try:
                            objects = object_detector.detect_boxes(
                                img_data, stream_id=session_id, stabilize=False, adaptive=True
                            )
                            detection_diag = object_detector.diagnostics()
                            log.info(
                                "Object detection session=%s count=%d pass=%s raw=%s/%s/%s frame=%sx%s",
                                session_id, len(objects), detection_diag.get("selected_pass", "none"),
                                detection_diag.get("primary_raw", 0),
                                detection_diag.get("fallback_raw", 0),
                                detection_diag.get("center_raw", 0),
                                img_data.shape[1], img_data.shape[0],
                            )
                        except Exception as exc:
                            detector_error = str(exc)
                            log.exception("Deteksi objek (on-demand) gagal")
                            objects = []
                    else:
                        detector_error = service_init_errors.get("Object detector") or "Object detector gagal diinisialisasi"
                        log.warning("Object detection diminta tetapi detector tidak aktif: %s", detector_error)

                    if object_detector and not objects and YOLO_DEBUG_SAVE_EMPTY:
                        try:
                            debug_dir = os.path.join(BASE_DIR, "captured_images", "yolo_empty")
                            os.makedirs(debug_dir, exist_ok=True)
                            debug_name = f"empty_{_safe_filename_token(session_id, 'session')}_{int(time.time() * 1000)}.jpg"
                            debug_path = os.path.join(debug_dir, debug_name)
                            cv2.imwrite(debug_path, img_data)
                            log.info("YOLO empty-frame debug saved: %s", debug_path)
                        except Exception as exc:
                            log.debug("Gagal menyimpan YOLO debug frame: %s", exc)

                    sessions.update(
                        session_id,
                        last_objects=objects,
                        last_vision_at=time.time(),
                    )

                    labels = object_detector.summarize(objects, max_items=6) if objects else []
                    if detector_error:
                        answer = (
                            "Deteksi objek sedang tidak aktif pada server. "
                            "Periksa log startup atau endpoint /api/health untuk melihat penyebabnya."
                        )
                    elif _is_simple_scene_question(transcript):
                        answer = (
                            "Saya mendeteksi " + ", ".join(labels) + " di depan Anda."
                            if labels else "Saya belum mendeteksi objek yang cukup jelas di depan Anda."
                        )
                    elif groq_ai:
                        answer = groq_ai.ask_with_context(
                            transcript or "Apa yang ada di depan saya?",
                            detected_objects=objects,
                        )
                    elif labels:
                        answer = "Saya mendeteksi " + ", ".join(labels) + " di depan Anda."
                    else:
                        answer = "Saya belum mendeteksi objek yang cukup jelas di depan Anda."

            elif mode == "followup":
                # Reuse the most recent detection result. No new camera frame is
                # decoded and no detector runs, which makes natural follow-ups cheap.
                objects = previous.get("last_objects") or []
                faces = previous.get("last_faces") or []
                sign_text = previous.get("last_sign_text") or ""
                sign_confidence = float(previous.get("last_sign_confidence") or 0.0)
                sign_reliable = bool(previous.get("last_sign_reliable"))
                sign_boxes = previous.get("last_signs") or []
                if groq_ai:
                    answer = groq_ai.ask_with_context(
                        transcript,
                        detected_objects=objects,
                        sign_text=sign_text,
                        sign_confidence=sign_confidence,
                        sign_reliable=sign_reliable,
                        context_is_cached=True,
                    )
                else:
                    labels = object_detector.summarize(objects, max_items=6) if objects else []
                    if sign_text and sign_reliable:
                        answer = "Hasil terakhir: " + spoken_text(sign_text)
                    elif labels:
                        answer = "Pada hasil kamera terakhir, saya mendeteksi " + ", ".join(labels) + "."
                    else:
                        answer = "Belum ada hasil kamera sebelumnya yang cukup jelas."

            else:
                # Pure conversation: zero camera decoding, YOLO, sign, or face model.
                answer = groq_ai.ask(transcript if transcript else "Halo") if groq_ai else "Layanan AI sedang tidak tersedia."

            sessions.update(session_id, mode=mode, last_result=answer)

            safe_sid = _safe_filename_token(session_id, "session")
            safe_rid = _safe_filename_token(request_id, "request")
            mp3_name = f"session_{safe_sid}_{safe_rid}.mp3"
            mp3_path = _save_tts(answer, mp3_name)

            device_id = sessions.device_for_session(session_id)
            device_socket = sessions.get_esp32_socket(device_id) if device_id else None
            audio_output = "esp32" if (device_socket and mp3_path) else "browser"
            response_data = {
                "status": "success" if mp3_path else "partial",
                "request_id": request_id,
                "mode": mode,
                "transcript": transcript,
                "stt_engine": stt_engine,
                "result": answer,
                "detected_objects": objects,
                "sign_text": sign_text,
                "sign_confidence": sign_confidence,
                "sign_reliable": sign_reliable,
                "sign_boxes": sign_boxes,
                "faces": faces,
                "audio_output": audio_output,
                "duplicate_suppressed": False,
                "object_detection_available": object_detector is not None,
                "object_detection_error": service_init_errors.get("Object detector"),
                "detection_diagnostics": detection_diag if mode == "deskripsi" else {},
            }
            if img_data is not None:
                response_data["frame_width"] = int(img_data.shape[1])
                response_data["frame_height"] = int(img_data.shape[0])
            if mp3_path:
                response_data["audio_url"] = f"/api/audio_file/{mp3_name}"
            else:
                response_data["audio_error"] = "TTS tidak tersedia; browser akan mencoba speech synthesis sebagai fallback"

            if detected_image is not None:
                ok, buf = cv2.imencode(".jpg", detected_image)
                if ok:
                    response_data["image_url"] = "data:image/jpeg;base64," + base64.b64encode(buf).decode("utf-8")

            if device_socket and mp3_path:
                esp32_ip = sessions.get_device_ip(device_id)
                if esp32_ip:
                    # Jangan blok response HTTP ke browser menunggu ESP32 selesai
                    # memutar audio (streamPcmToI2S di firmware bisa makan beberapa detik).
                    threading.Thread(
                        target=push_mp3_to_esp32,
                        args=(mp3_path.read_bytes(), esp32_ip),
                        daemon=True,
                    ).start()
                else:
                    log.warning("Tidak bisa push audio ke %s: IP belum diketahui.", device_id)

            if database:
                database.save_interaction({
                    "prompt": transcript,
                    "response": answer,
                    "mode": mode,
                    "session_id": session_id,
                    "request_id": request_id,
                    "stt_engine": stt_engine,
                    "detected_objects": objects,
                    "sign_text": sign_text,
                    "sign_confidence": sign_confidence,
                    "sign_reliable": sign_reliable,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })

            sessions.finish_request(session_id, request_id, prompt_key, response_data)
            return jsonify(response_data)

        except Exception as exc:
            if session_id and request_claimed:
                sessions.fail_request(session_id, request_id)
            log.error(f"Error saat /api/upload: {exc}")
            return jsonify({"status": "error", "message": str(exc)}), 500


    @app.route("/api/audio_file/<path:filename>", methods=["GET"])
    def audio_file(filename):
        safe_name = os.path.basename(filename)
        if not os.path.isfile(os.path.join(audio_dir, safe_name)):
            return abort(404, "Audio belum tersedia")
        return send_from_directory(audio_dir, safe_name, mimetype="audio/mpeg", conditional=False)

    @app.route("/mp3", methods=["GET"])
    def latest_mp3():
        """MP3 terbaru (kompatibilitas klien lama yang menarik satu URL tetap)."""
        if not audio_processor:
            return abort(503, "Audio service unavailable")
        latest = audio_processor.get_latest_audio()
        if not latest:
            return abort(404, "No audio available")
        response = send_from_directory(audio_dir, latest, mimetype="audio/mpeg", conditional=False)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.route("/api/session/<session_id>", methods=["GET"])
    def session_status(session_id):
        state = sessions.get(session_id)
        if not state:
            return jsonify({"status": "error", "message": "Session tidak ditemukan"}), 404
        return jsonify({"status": "success", "session": state})
