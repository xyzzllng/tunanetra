"""Backward-compatible audio facade used by both Flask servers."""
from __future__ import annotations

import base64
import os
from datetime import datetime
from threading import Lock

import cv2

from config.settings import settings
from services.speech import SpeechService


class AudioProcessor:
    def __init__(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.AUDIO_DIR = os.path.join(base_dir, "audio_storage")
        os.makedirs(self.AUDIO_DIR, exist_ok=True)
        self.lock = Lock()
        self.last_audio_path = None
        self.last_played = None
        self.speech = SpeechService()

    @property
    def stt_available(self):
        return self.speech.stt_available

    def process_audio_input(self, audio_data, filename="input.wav"):
        """Compatibility method: transcribe one audio payload with Groq Whisper."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        suffix = os.path.splitext(filename)[1] or ".wav"
        audio_path = os.path.join(self.AUDIO_DIR, f"input_{timestamp}{suffix}")
        with open(audio_path, "wb") as f:
            f.write(audio_data)
        try:
            prompt = self.speech.transcribe_bytes(audio_data, filename=filename)
            return prompt, audio_path
        except Exception as exc:
            return f"Error: {exc}", audio_path

    def generate_tts(self, text, lang=None):
        return self.speech.synthesize(text, lang=lang or settings.tts_language)

    def _play_audio(self, path):
        return self.speech.play_file_async(path)

    def _write_tts(self, text, mp3_path):
        audio_buffer = self.generate_tts(text)
        with open(mp3_path, "wb") as f:
            f.write(audio_buffer.getbuffer())
        self.last_audio_path = mp3_path
        return mp3_path

    def process_mode_response(self, mode, result, detected_image=None):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        mp3_path = os.path.join(self.AUDIO_DIR, f"mode_{mode}_{timestamp}.mp3")
        self._write_tts(result, mp3_path)
        self._play_audio(mp3_path)

        response_data = {
            "status": "success",
            "result": result,
            "audio_path": mp3_path,
            "audio_url": "/mp3",
            "local_playback_requested": bool(settings.local_audio_playback),
        }
        if detected_image is not None:
            ok, buffer = cv2.imencode(".jpg", detected_image)
            if ok:
                response_data["image_url"] = "data:image/jpeg;base64," + base64.b64encode(buffer).decode("utf-8")
        self._cleanup_old_files()
        return response_data

    def generate_full_response(self, prompt, answer):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        mp3_path = os.path.join(self.AUDIO_DIR, f"response_{timestamp}.mp3")
        self._write_tts(answer, mp3_path)
        self._play_audio(mp3_path)
        self._cleanup_old_files()
        return {
            "status": "success",
            "audio_url": "/mp3",
            "transcript": prompt,
            "response": answer,
            "audio_path": mp3_path,
            "local_playback_requested": bool(settings.local_audio_playback),
        }

    def _cleanup_old_files(self, max_files=10):
        # Keep recent output audio and recent STT uploads only; long-running
        # deployments should not grow audio_storage without bound.
        names = [
            f for f in os.listdir(self.AUDIO_DIR)
            if f.endswith((".mp3", ".wav", ".webm", ".ogg", ".m4a", ".mp4"))
        ]
        if len(names) <= max_files:
            return
        names.sort(key=lambda f: os.path.getmtime(os.path.join(self.AUDIO_DIR, f)))
        for old_file in names[:-max_files]:
            try:
                os.remove(os.path.join(self.AUDIO_DIR, old_file))
            except OSError:
                pass

    def get_latest_audio(self):
        mp3_files = [f for f in os.listdir(self.AUDIO_DIR) if f.endswith(".mp3")]
        if not mp3_files:
            return None
        return max(mp3_files, key=lambda f: os.path.getmtime(os.path.join(self.AUDIO_DIR, f)))
