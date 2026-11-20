"""Push audio TTS (MP3) ke speaker ESP32 lewat HTTP POST /tts/play.

Firmware ESP32 terbaru (lihat firmware/esp32_multidevice/esp32_spider.ino)
sudah menghapus total jalur PULL (``tts_ready`` + URL, ESP32 download MP3
sendiri). Satu-satunya cara memutar audio sekarang adalah PUSH: backend
mengonversi MP3 menjadi raw PCM 16-bit signed little-endian, 44100 Hz,
stereo, lalu mem-POST byte itu langsung ke ``http://<ip-esp32>:8080/tts/play``.

Modul ini adalah versi "library-friendly" dari ``tools/esp32_pcm_push.py``
(logic sama), supaya bisa di-import langsung dari services/quick_commands.py
dan devices/ws.py tanpa menduplikasi kode.

Dependensi: pydub (butuh ffmpeg terpasang di sistem/PATH).
    pip install pydub
"""
from __future__ import annotations

import io
import logging
import threading
import time

import requests

from config.settings import settings

log = logging.getLogger(__name__)

try:
    from pydub import AudioSegment
except ImportError:  # pragma: no cover - dependency belum terpasang
    AudioSegment = None

TARGET_SAMPLE_RATE = 44100   # HARUS sama dengan AUDIO_SAMPLE_RATE di firmware
TARGET_CHANNELS = 2
TARGET_SAMPLE_WIDTH = 2      # 16-bit

DEFAULT_HTTP_PORT = 8080
DEFAULT_ENDPOINT = "/tts/play"
REQUEST_TIMEOUT_SEC = 20


def mp3_bytes_to_esp32_pcm(mp3_bytes: bytes) -> bytes:
    """Mengubah MP3 (bytes) menjadi raw PCM 44.1kHz/stereo/16-bit tanpa header,
    sesuai kontrak firmware (lihat AUDIO_SAMPLE_RATE/AUDIO_CHANNELS/AUDIO_BITS
    di esp32_spider.ino)."""
    if AudioSegment is None:
        raise RuntimeError(
            "Modul 'pydub' belum terpasang. Jalankan: pip install pydub "
            "(dan pastikan ffmpeg ada di PATH)."
        )
    audio = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
    audio = (
        audio
        .set_frame_rate(TARGET_SAMPLE_RATE)
        .set_channels(TARGET_CHANNELS)
        .set_sample_width(TARGET_SAMPLE_WIDTH)
    )
    return audio.raw_data


def send_pcm_to_esp32(pcm_bytes: bytes, esp32_ip: str, port: int = DEFAULT_HTTP_PORT,
                       token: str | None = None, timeout: float = REQUEST_TIMEOUT_SEC) -> bool:
    """POST raw PCM ke endpoint /tts/play di ESP32. True bila ESP32 balas 200."""
    if not pcm_bytes:
        log.warning("[esp32_audio_push] audio kosong, tidak dikirim.")
        return False
    if not esp32_ip:
        log.warning("[esp32_audio_push] IP ESP32 tidak diketahui, audio tidak dikirim.")
        return False

    url = f"http://{esp32_ip}:{port}{DEFAULT_ENDPOINT}"
    headers = {"Content-Type": "application/octet-stream"}
    if token:
        headers["X-Auth-Token"] = token

    thread_name = threading.current_thread().name
    t_start = time.monotonic()
    log.info("[esp32_audio_push] mulai POST ke %s | %d byte PCM | thread=%s",
              url, len(pcm_bytes), thread_name)

    try:
        response = requests.post(url, data=pcm_bytes, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        elapsed_ms = (time.monotonic() - t_start) * 1000
        # exc.args[0] biasanya bungkus urllib3, ulik ke exception asli (mis. WinError
        # 10061 = connection refused) supaya kelihatan akar masalahnya.
        root_cause = exc.__cause__ or exc.__context__ or exc
        log.warning(
            "[esp32_audio_push] GAGAL CONNECT ke %s:%s setelah %.0fms | thread=%s | "
            "jenis=%s | detail=%r | root_cause=%r",
            esp32_ip, port, elapsed_ms, thread_name, type(exc).__name__, exc, root_cause)
        return False
    except requests.exceptions.Timeout as exc:
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log.warning(
            "[esp32_audio_push] TIMEOUT setelah %.0fms menunggu ESP32 (%s:%s) | thread=%s | detail=%r",
            elapsed_ms, esp32_ip, port, thread_name, exc)
        return False
    except requests.exceptions.RequestException as exc:
        elapsed_ms = (time.monotonic() - t_start) * 1000
        log.warning(
            "[esp32_audio_push] error tak terduga setelah %.0fms saat mengirim audio ke %s:%s | "
            "thread=%s | jenis=%s | detail=%r",
            elapsed_ms, esp32_ip, port, thread_name, type(exc).__name__, exc)
        return False

    elapsed_ms = (time.monotonic() - t_start) * 1000

    if response.status_code != 200:
        log.warning("[esp32_audio_push] ESP32 menolak/gagal memutar audio (status %s) setelah %.0fms: %s",
                    response.status_code, elapsed_ms, response.text)
        return False

    log.info("[esp32_audio_push] SUKSES diputar di %s dalam %.0fms", esp32_ip, elapsed_ms)
    return True


def push_mp3_to_esp32(mp3_bytes: bytes, esp32_ip: str, port: int = DEFAULT_HTTP_PORT,
                       retries: int = 2, retry_delay: float = 1.0) -> bool:
    """Fungsi tingkat tinggi: MP3 bytes -> konversi PCM -> kirim ke ESP32.

    ``token`` diambil otomatis dari settings.esp32_shared_token (harus sama
    dengan ESP32_TOKEN di firmware/*/secrets.h).

    Otomatis retry (default 2x, jeda 1 detik) bila koneksi gagal/di-reset.
    Ini menangani kondisi sesaat di ESP32 (mis. heap sempat rendah karena
    Bluetooth A2DP) yang biasanya sudah pulih dalam 1-2 detik - terlihat
    dari heap ESP32 yang naik-turun tajam (mis. 8KB -> 30KB dalam ~3 detik)
    saat push sempat gagal dengan ConnectionResetError (WinError 10054).
    """
    try:
        pcm_bytes = mp3_bytes_to_esp32_pcm(mp3_bytes)
    except Exception as exc:
        log.warning("[esp32_audio_push] gagal konversi MP3 -> PCM: %s", exc)
        return False

    attempt = 0
    while True:
        attempt += 1
        ok = send_pcm_to_esp32(pcm_bytes, esp32_ip=esp32_ip, port=port,
                                token=settings.esp32_shared_token or None)
        if ok:
            return True
        if attempt > retries:
            log.warning("[esp32_audio_push] menyerah setelah %d percobaan ke %s", attempt, esp32_ip)
            return False
        log.info("[esp32_audio_push] retry %d/%d ke %s dalam %.1fs...",
                  attempt, retries, esp32_ip, retry_delay)
        time.sleep(retry_delay)