"""Speech-to-text and text-to-speech services.

STT uses Groq Whisper. TTS outputs MP3 using Edge TTS with a gTTS fallback.
This keeps Indonesian speech reliable and directly playable by browsers and
ESP32-audioI2S without adding an audio-conversion dependency.
"""
from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from pathlib import Path
from typing import Optional

from gtts import gTTS
from groq import Groq

from config.settings import settings

try:
    import edge_tts  # type: ignore
except Exception:  # optional at import time; gTTS remains a fallback
    edge_tts = None

try:
    import pygame  # type: ignore
except Exception:  # local playback is optional for headless/server deployments
    pygame = None


class SpeechService:
    def __init__(self) -> None:
        self.groq_client: Optional[Groq] = None
        self.last_error: str | None = None
        self._playback_lock = threading.Lock()
        self._mixer_ready = False

        if settings.groq_api_key:
            try:
                self.groq_client = Groq(
                    api_key=settings.groq_api_key,
                    timeout=settings.groq_timeout_seconds,
                    max_retries=1,
                )
            except Exception as exc:
                self.last_error = str(exc)

    @property
    def stt_available(self) -> bool:
        return self.groq_client is not None

    def transcribe_bytes(self, audio_bytes: bytes, filename: str = "speech.webm") -> str:
        if not audio_bytes:
            raise ValueError("Audio kosong")
        if not self.groq_client:
            raise RuntimeError("Groq STT tidak tersedia. Set GROQ_API_KEY terlebih dahulu.")

        suffix = Path(filename or "speech.webm").suffix.lower()
        if suffix not in {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}:
            filename = "speech.webm"

        try:
            result = self.groq_client.audio.transcriptions.create(
                file=(os.path.basename(filename), audio_bytes),
                model=settings.groq_stt_model,
                language=settings.stt_language or None,
                response_format="json",
                temperature=0.0,
            )
            text = (getattr(result, "text", None) or "").strip()
            if not text:
                raise RuntimeError("Groq tidak menghasilkan transcript")
            return text
        except Exception as exc:
            self.last_error = str(exc)
            raise RuntimeError(f"STT Groq gagal: {exc}") from exc

    @staticmethod
    def _run_async(coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        # Defensive path if called from a thread that already owns an event loop.
        result = {}
        error = {}

        def runner():
            try:
                result["value"] = asyncio.run(coro)
            except Exception as exc:  # pragma: no cover - rare runtime topology
                error["value"] = exc

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join()
        if "value" in error:
            raise error["value"]
        return result.get("value")

    async def _edge_tts_bytes(self, text: str) -> bytes:
        if edge_tts is None:
            raise RuntimeError("edge-tts belum terpasang")
        communicate = edge_tts.Communicate(text=text, voice=settings.tts_voice, connect_timeout=5, receive_timeout=15)
        chunks = []
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                chunks.append(chunk.get("data", b""))
        data = b"".join(chunks)
        if not data:
            raise RuntimeError("Edge TTS tidak menghasilkan audio")
        return data

    @staticmethod
    def _gtts_bytes(text: str, lang: str = "id") -> bytes:
        buf = io.BytesIO()
        gTTS(text=text, lang=lang, timeout=(5, 15)).write_to_fp(buf)
        return buf.getvalue()

    def synthesize(self, text: str, lang: str | None = None) -> io.BytesIO:
        clean = " ".join((text or "").split()).strip()
        if not clean:
            raise ValueError("Teks TTS kosong")

        language = (lang or settings.tts_language or "id-ID").strip()
        provider = settings.tts_provider
        errors: list[str] = []

        if provider in {"auto", "edge"}:
            try:
                return io.BytesIO(self._run_async(self._edge_tts_bytes(clean)))
            except Exception as exc:
                errors.append(f"edge={exc}")

        if provider in {"auto", "edge", "gtts"}:
            try:
                short_lang = language.split("-")[0].lower() or "id"
                return io.BytesIO(self._gtts_bytes(clean, lang=short_lang))
            except Exception as exc:
                errors.append(f"gtts={exc}")

        self.last_error = "; ".join(errors) or f"Provider TTS tidak dikenal: {provider}"
        raise RuntimeError(self.last_error)

    def play_file_async(self, path: str) -> bool:
        """Play an MP3 through the server's local audio device without blocking."""
        if not settings.local_audio_playback or pygame is None:
            return False
        if not os.path.isfile(path):
            return False

        def worker():
            with self._playback_lock:
                try:
                    if not self._mixer_ready:
                        pygame.mixer.init()
                        self._mixer_ready = True
                    pygame.mixer.music.stop()
                    pygame.mixer.music.load(path)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
                        time.sleep(0.05)
                except Exception as exc:
                    self.last_error = f"Local playback gagal: {exc}"

        threading.Thread(target=worker, daemon=True).start()
        return True
