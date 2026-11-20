"""
esp32_pcm_push.py
====================
Layer komunikasi TAMBAHAN antara sistem AI webcam (Groq/MongoDB/Ubidots)
yang sudah ada dengan speaker ESP32.

Alur:
    Webcam -> deteksi -> Groq (AI) -> teks respons   [SUDAH ADA, tidak diubah]
    teks respons -> TTS di komputer -> PCM 44.1kHz stereo -> HTTP -> ESP32
    ESP32 -> memutar lewat speaker I2S                [firmware baru]

Cara pakai di program webcam yang sudah ada:

    from esp32_tts_client import speak_via_esp32

    # ... kode Groq/MongoDB/Ubidots yang sudah ada, tidak diubah ...
    ai_response_text = groq_response.choices[0].message.content

    # baris BARU satu-satunya yang perlu ditambahkan:
    speak_via_esp32(ai_response_text, esp32_ip="192.168.1.50")

Instalasi dependensi:
    pip install pydub gTTS requests
    # pydub butuh ffmpeg terpasang di sistem (bukan lewat pip):
    #   Windows : download ffmpeg, tambahkan ke PATH
    #   Linux   : sudo apt install ffmpeg
    #   macOS   : brew install ffmpeg

Jika tidak ingin bergantung pada internet (gTTS butuh internet), ganti
fungsi `_text_to_speech_file` untuk memakai TTS offline seperti pyttsx3,
atau layanan TTS lain yang sudah dipakai di proyek Anda.
"""

import io
import os
import threading
import wave

import requests

try:
    from pydub import AudioSegment
except ImportError as exc:
    raise ImportError(
        "Modul 'pydub' belum terpasang. Jalankan: pip install pydub "
        "(dan pastikan ffmpeg terpasang di sistem)."
    ) from exc


# ==============================================================================
# KONFIGURASI
# ==============================================================================

TARGET_SAMPLE_RATE = 44100   # HARUS sama dengan konfigurasi I2S di firmware ESP32
TARGET_CHANNELS = 2          # stereo, sesuai firmware
TARGET_SAMPLE_WIDTH = 2      # 16-bit

DEFAULT_HTTP_PORT = 8080
DEFAULT_ENDPOINT = "/tts/play"

MAX_TEXT_CHARS = 400         # batas panjang teks per kalimat yang dikirim
REQUEST_TIMEOUT_SEC = 20

# Kunci global supaya audio TTS tidak saling tumpang tindih (butir #12 spesifikasi)
_playback_lock = threading.Lock()


# ==============================================================================
# TEXT-TO-SPEECH (SISI KOMPUTER)
# ==============================================================================

def _text_to_speech_file(text: str, out_path: str, lang: str = "id") -> None:
    """
    Mengubah teks menjadi file audio (mp3) memakai gTTS.
    Ganti fungsi ini jika Anda sudah memakai backend TTS lain (mis. ElevenLabs,
    Azure TTS, PlayAI/Groq TTS, atau pyttsx3 untuk mode offline).
    """
    from gtts import gTTS

    tts = gTTS(text=text, lang=lang)
    tts.save(out_path)


# ==============================================================================
# KONVERSI KE FORMAT YANG DIMENGERTI ESP32
# ==============================================================================

def convert_to_esp32_pcm(audio_path: str) -> bytes:
    """
    Membaca file audio apa pun yang didukung ffmpeg (mp3/wav/ogg/dst),
    lalu mengubahnya menjadi raw PCM 16-bit signed little-endian,
    44100 Hz, stereo -- TANPA header WAV -- sesuai kontrak firmware ESP32.
    """

    audio = AudioSegment.from_file(audio_path)

    audio = (
        audio
        .set_frame_rate(TARGET_SAMPLE_RATE)
        .set_channels(TARGET_CHANNELS)
        .set_sample_width(TARGET_SAMPLE_WIDTH)
    )

    return audio.raw_data


def wav_bytes_to_esp32_pcm(wav_bytes: bytes) -> bytes:
    """
    Alternatif convert_to_esp32_pcm() untuk kasus audio sudah berupa
    bytes WAV di memori (tanpa file di disk).
    """

    buffer = io.BytesIO(wav_bytes)
    audio = AudioSegment.from_file(buffer, format="wav")

    audio = (
        audio
        .set_frame_rate(TARGET_SAMPLE_RATE)
        .set_channels(TARGET_CHANNELS)
        .set_sample_width(TARGET_SAMPLE_WIDTH)
    )

    return audio.raw_data


# ==============================================================================
# PENGIRIMAN KE ESP32
# ==============================================================================

def send_pcm_to_esp32(
    pcm_bytes: bytes,
    esp32_ip: str,
    port: int = DEFAULT_HTTP_PORT,
    token: str | None = None,
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> bool:
    """
    Mengirim data PCM mentah ke endpoint HTTP /tts/play di ESP32.
    Mengembalikan True jika ESP32 mengonfirmasi audio berhasil diputar.

    Penanganan error (sesuai butir #13 spesifikasi):
      - ESP32 tidak terhubung / IP salah  -> ConnectionError ditangkap, return False
      - Timeout (WiFi lambat/ESP32 sibuk) -> Timeout ditangkap, return False
      - Audio kosong                      -> ditolak sebelum request dikirim
    """

    if not pcm_bytes:
        print("[esp32_tts] audio kosong, tidak dikirim.")
        return False

    url = f"http://{esp32_ip}:{port}{DEFAULT_ENDPOINT}"

    headers = {"Content-Type": "application/octet-stream"}
    if token:
        headers["X-Auth-Token"] = token

    # Hanya satu audio yang boleh diputar dalam satu waktu
    with _playback_lock:
        try:
            response = requests.post(
                url,
                data=pcm_bytes,
                headers=headers,
                timeout=timeout,
            )
        except requests.exceptions.ConnectionError:
            print(f"[esp32_tts] gagal terhubung ke ESP32 di {esp32_ip}:{port}.")
            return False
        except requests.exceptions.Timeout:
            print("[esp32_tts] timeout menunggu ESP32 selesai memutar audio.")
            return False
        except requests.exceptions.RequestException as exc:
            print(f"[esp32_tts] error tak terduga saat mengirim audio: {exc}")
            return False

    if response.status_code != 200:
        print(
            f"[esp32_tts] ESP32 menolak/gagal memutar audio "
            f"(status {response.status_code}): {response.text}"
        )
        return False

    return True


# ==============================================================================
# FUNGSI UTAMA - DIPANGGIL DARI PROGRAM WEBCAM YANG SUDAH ADA
# ==============================================================================

def _split_long_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> list[str]:
    """Memecah teks panjang per kalimat agar setiap request tetap ringan."""

    text = text.strip()

    if len(text) <= max_chars:
        return [text] if text else []

    sentences = text.replace("!", ".").replace("?", ".").split(".")
    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        candidate = f"{current} {sentence}.".strip()

        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = f"{sentence}."
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


def speak_via_esp32(
    text: str,
    esp32_ip: str,
    port: int = DEFAULT_HTTP_PORT,
    token: str | None = None,
    lang: str = "id",
) -> bool:
    """
    Fungsi tingkat tinggi: teks respons AI -> TTS -> kirim ke ESP32 -> putar.

    Ini SATU-SATUNYA fungsi yang perlu dipanggil dari program webcam Anda
    yang sudah ada, tepat setelah mendapatkan teks respons dari Groq.

    Menangani teks kosong dan teks terlalu panjang sesuai butir #13:
      - teks kosong        -> tidak melakukan apa pun, return False
      - teks terlalu panjang -> dipecah per kalimat dan dikirim berurutan
    """

    if not text or not text.strip():
        print("[esp32_tts] teks respons AI kosong, dilewati.")
        return False

    chunks = _split_long_text(text)
    all_success = True

    for chunk in chunks:

        tmp_path = "_tts_tmp.mp3"

        try:
            _text_to_speech_file(chunk, tmp_path, lang=lang)
            pcm_bytes = convert_to_esp32_pcm(tmp_path)
        except Exception as exc:
            print(f"[esp32_tts] gagal melakukan TTS/konversi audio: {exc}")
            all_success = False
            continue
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        success = send_pcm_to_esp32(
            pcm_bytes,
            esp32_ip=esp32_ip,
            port=port,
            token=token,
        )

        all_success = all_success and success

    return all_success


# ==============================================================================
# CONTOH PEMAKAIAN MANDIRI (python esp32_pcm_push.py)
# ==============================================================================

if __name__ == "__main__":

    ESP32_IP = "192.168.1.50"   # ganti sesuai IP ESP32 di jaringan Anda
    ESP32_TOKEN = None          # isi jika ESP32_TOKEN di secrets.h tidak kosong

    ok = speak_via_esp32(
        "Objek terdeteksi di depan Anda, jarak sekitar setengah meter.",
        esp32_ip=ESP32_IP,
        token=ESP32_TOKEN,
    )

    print("Berhasil diputar di ESP32" if ok else "Gagal mengirim/memutar audio")
