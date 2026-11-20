> **Arsip.** Dokumen sejarah sebelum migrasi ke ESP32-CAM; path dan alur (browser/webcam) merujuk tata letak lama. Lihat `docs/MIGRASI.md` untuk pemetaan file lama → baru.

# Dokumentasi lama — sebelum Quick Command

Dokumen ini disimpan sebagai referensi integrasi lama. Untuk instalasi, default
threshold, model tambahan, dan UI yang berlaku sekarang, baca `../README.md`.

Project ini adalah hasil migrasi dari Gemini ke **Groq** dengan integrasi **MongoDB** dan **Ubidots** tetap dipertahankan. Alur kamera dibuat on-demand agar YOLO/OCR tidak berjalan terus-menerus.

## Arsitektur utama

```text
Microphone (browser / input audio)
        |
        v
Groq Whisper STT
        |
        v
Command routing
  |         |          |
  |         |          +--> percakapan biasa -> Groq LLM (tanpa CV)
  |         +-------------> pertanyaan teks -> 1 snapshot -> EasyOCR -> Groq
  +-----------------------> pertanyaan objek -> 1 snapshot -> YOLOv5 -> Groq/fast response
        |
        v
Text response
        |
        v
TTS (id-ID: Edge TTS -> gTTS fallback)
        |
        +--> browser audio
        +--> ESP32/MAX98357A jika device terhubung
        +--> local speaker pada server.py jika LOCAL_AUDIO_PLAYBACK=true
```

Untuk pertanyaan lanjutan seperti **"jelaskan yang tadi"**, server memakai cache hasil detection/OCR terakhir tanpa inference kamera baru.

## Perubahan penting

- Gemini dihapus dari runtime dan diganti `GroqAI` (`ai/provider.py`).
- Default LLM: `openai/gpt-oss-20b`.
- STT utama: Groq `whisper-large-v3-turbo`.
- Browser tidak lagi memakai Web Speech API sebagai STT utama; audio push-to-talk direkam dengan `MediaRecorder` dan dikirim ke backend.
- TTS benar-benar menghasilkan MP3 dan diarahkan ke browser/ESP32; `server.py` juga dapat memutar hasil lewat local speaker menggunakan `pygame`.
- TTS Bahasa Indonesia memakai Edge TTS, dengan gTTS sebagai fallback, sehingga output tetap berupa MP3 yang dapat langsung diputar browser/ESP32.
- Kamera tidak menjalankan inference per frame. Snapshot hanya diproses jika hasil STT menunjukkan perintah visual.
- YOLO mendapatkan filtering tambahan untuk bounding box sangat kecil / low-confidence serta stabilizer untuk endpoint realtime/manual.
- OCR menjalankan fast pass terlebih dahulu; rotation/threshold second-pass hanya ketika hasil awal kurang yakin.
- MongoDB dan Ubidots tetap independen: kegagalan salah satu layanan tidak membuat request utama crash.
- Credential firmware ESP32 dipindahkan ke `esp32_multidevice/secrets.h` (ignored Git).

## Struktur

```text
camdetection_fixed/
├── main.py
├── server.py                    # external/local webcam mode
├── server_multidevice.py        # browser/HP camera + ESP32 pairing (recommended)
├── session_manager.py
├── config/
│   └── settings.py
├── ai/
│   ├── provider.py              # Groq LLM abstraction
│   ├── tanys.py                 # compatibility shim; Gemini sudah dihapus
│   ├── deteksiObjek.py
│   ├── bacaTex.py
│   └── deteksiWajah.py
├── speech/
│   └── service.py               # Groq STT + TTS providers
├── services/
│   └── database.py              # MongoDB + Ubidots
├── app_utils/
│   ├── audio.py                 # compatibility facade + local playback
│   └── camera.py                # on-demand webcam adapter
├── model/
│   ├── yolov5n.pt
│   ├── yolov5/                  # local YOLOv5 source
│   ├── emotion_model.pth
│   └── haarcascade_frontalface_default.xml
├── web/
├── esp32_multidevice/
├── requirements.txt
├── .env.example
└── .gitignore
```

## 1. Persyaratan

Disarankan:

- Python **3.11**
- Windows 10/11, Linux, atau Raspberry Pi yang mampu menjalankan PyTorch/EasyOCR
- Browser modern dengan `MediaRecorder`, camera, dan microphone permission
- FFmpeg tidak wajib untuk alur browser standar karena Groq menerima WebM/OGG/M4A/WAV secara langsung

## 2. Instalasi Python

Windows PowerShell:

```powershell
cd path\ke\camdetection_fixed
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Linux/macOS:

```bash
cd /path/to/camdetection_fixed
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> PyTorch/torchvision sangat bergantung pada OS, CPU/GPU, dan CUDA. Jika Anda memakai CUDA, instal pasangan `torch` + `torchvision` yang sesuai dari panduan resmi PyTorch sebelum menjalankan `pip install -r requirements.txt`.

## 3. Environment variable

Salin template:

```bash
cp .env.example .env
```

Windows:

```powershell
Copy-Item .env.example .env
```

Minimal isi:

```env
GROQ_API_KEY=your_api_key
MONGODB_URI=your_mongodb_uri
UBIDOTS_TOKEN=your_ubidots_token
```

Tidak ada API key/token/password yang perlu ditulis di source Python.

### Model Groq

Default project:

```env
GROQ_MODEL=openai/gpt-oss-20b
GROQ_STT_MODEL=whisper-large-v3-turbo
```

Jika model pada akun Groq Anda memiliki permission berbeda, ubah melalui `.env`, bukan source code.

## 4. Menjalankan mode multi-device (direkomendasikan)

```bash
python main.py
```

`SERVER_MODE=multi` adalah default. Server listen pada `0.0.0.0:5000`.

Buka dari HP/browser menggunakan IP LAN komputer, misalnya:

```text
http://192.168.1.10:5000
```

Untuk akses camera/microphone pada browser mobile, beberapa browser mewajibkan HTTPS kecuali origin tertentu. Gunakan HTTPS/reverse proxy bila permission browser menolaknya.

## 5. Menjalankan mode webcam lokal

Set:

```env
SERVER_MODE=single
WEBCAM_SOURCE=0
LOCAL_AUDIO_PLAYBACK=true
```

Lalu:

```bash
python main.py
```

Pada mode ini `app_utils/camera.py` tidak membaca webcam terus-menerus. Camera handle tetap siap, lalu burst kecil diambil hanya saat `capture()` dipanggil.

## 6. Alur speech-to-speech

Mode multi-device sekarang bekerja seperti ini:

1. User menahan tombol bicara.
2. Browser merekam audio melalui `MediaRecorder`.
3. Saat tombol dilepas, satu clip audio dikirim ke `/api/upload`.
4. Backend menjalankan Groq Whisper STT.
5. Router menentukan apakah prompt perlu kamera.
6. Jika tidak perlu kamera: tidak ada decode image, YOLO, OCR, atau face detection.
7. Jika perlu kamera: hanya detector yang relevan dijalankan.
8. Groq membuat jawaban natural.
9. TTS membuat MP3.
10. Jika ESP32 online dan paired, server mengirim satu event `tts_ready`; jika tidak, browser memutar MP3.
11. Jika MP3 gagal diputar, browser memakai `speechSynthesis` sebagai fallback terakhir.

## 7. Object detection

Default model tetap `model/yolov5n.pt` agar kompatibel dengan project lama dan ringan untuk CPU.

Tuning utama di `.env`:

```env
YOLO_CONFIDENCE=0.25
YOLO_HIGH_CONFIDENCE=0.65
YOLO_SMALL_OBJECT_CONFIDENCE=0.28
YOLO_IMAGE_SIZE=640
YOLO_MIN_AREA_RATIO=0.00003
YOLO_SMALL_AREA_RATIO=0.0010
YOLO_MIN_BOX_PX=4
YOLO_ADAPTIVE_FALLBACK=true
YOLO_FALLBACK_CONFIDENCE=0.14
YOLO_FALLBACK_IMAGE_SIZE=768
YOLO_CENTER_CROP_FALLBACK=true
YOLO_CENTER_CROP_RATIO=0.72
```

Object detection memakai tiga tingkat yang hanya aktif sesuai kebutuhan: primary full-frame, fallback full-frame yang lebih sensitif jika primary kosong, lalu center-crop recovery jika keduanya kosong. Input OpenCV dikonversi BGR ke RGB sebelum masuk YOLOv5. Jika terlalu banyak false positive, naikkan `YOLO_CONFIDENCE` sedikit (mis. 0.30-0.35) dan pertahankan fallback untuk recovery.

Endpoint `/api/detect_frame` tetap tersedia untuk manual/realtime testing dan memakai temporal stabilization. Alur voice normal tidak menunggu beberapa frame; satu prompt hanya memakai satu inference.

## 8. OCR

OCR memakai EasyOCR `en+id` dengan:

- resize adaptif
- grayscale
- CLAHE contrast enhancement
- bilateral denoise
- sharpening
- adaptive threshold fallback
- reading-order reconstruction
- duplicate suppression
- confidence/reliability gate

Fast pass tidak mencoba rotasi 90/180/270. Rotated second-pass hanya dijalankan saat hasil awal kosong atau confidence rendah, sehingga latency normal lebih kecil.

## 9. MongoDB dan Ubidots

Database lama tetap menggunakan:

- DB `SPIDER-SENSE`
- collection `Interaksi`
- collection `PenggunaanAlat`
- collection `DataEmosi`

Payload dashboard yang ada dipertahankan. `MONGODB_URI` juga memiliki fallback kompatibilitas ke nama lama `MONGO_URI` di `config/settings.py`.

Ubidots menggunakan device label default `spider-sense`, dapat diubah dengan:

```env
UBIDOTS_DEVICE_LABEL=spider-sense
```

Network error ditangkap dan tidak menjatuhkan Flask server.

## 10. ESP32

Masuk ke folder `esp32_multidevice/`, lalu salin:

```text
secrets.h.example -> secrets.h
```

Isi Wi-Fi, IP server, port, dan DEVICE_ID di `secrets.h`. File tersebut sudah masuk `.gitignore`.

Library firmware tetap:

- WebSockets (Markus Sattler)
- ArduinoJson
- ESP32-audioI2S (schreibfaul1)

Speaker MAX98357A tetap menerima URL MP3 dari server dan melakukan decode audio melalui `ESP32-audioI2S`.

## 11. Health check

Setelah server berjalan:

```text
GET /api/health
```

Gunakan response tersebut untuk melihat apakah Groq, OCR, object detector, MongoDB, dan konfigurasi Ubidots berhasil diinisialisasi.

## 12. Catatan TTS Groq

Groq menyediakan endpoint TTS, tetapi model TTS Groq yang tersedia saat project ini diperbarui berfokus pada English/Arabic. Karena project memakai Bahasa Indonesia, default `TTS_PROVIDER=auto` memilih Edge TTS `id-ID`, lalu fallback ke gTTS. Groq tetap menjadi AI utama dan STT utama.

Referensi Groq:

- https://console.groq.com/docs/text-chat
- https://console.groq.com/docs/speech-to-text
- https://console.groq.com/docs/text-to-speech/
- https://console.groq.com/docs/deprecations

## 13. Troubleshooting singkat

**Groq tidak aktif**: cek `GROQ_API_KEY`, koneksi internet, model permission, dan `/api/health`.

**Tidak ada suara browser**: pastikan volume tidak mute dan browser mengizinkan audio. Project otomatis fallback ke browser `speechSynthesis` jika MP3 gagal diputar.

**ESP32 tidak berbunyi**: cek pairing DEVICE_ID, socket ESP32, `SERVER_HOST`, jaringan yang sama, MAX98357A/I2S, dan apakah URL `/api/audio_file/...` dapat diakses dari ESP32.

**OCR lambat**: pastikan `OCR_GPU=false` jika tidak punya GPU CUDA dan jangan aktifkan `RUN_OCR_ON_SCENE` kecuali benar-benar diperlukan.

**YOLO lambat di CPU**: tetap gunakan `yolov5n.pt`, `YOLO_IMAGE_SIZE=640`, dan hindari endpoint realtime saat pemakaian normal.


## 12. Validasi yang dilakukan pada hasil migrasi

Sebelum project dikemas, dilakukan pemeriksaan statis terhadap seluruh source Python aplikasi (di luar source vendor YOLOv5), pemeriksaan syntax `web/app.js`, pengecekan target import lokal, pengecekan archive ZIP, serta scan agar credential/Gemini runtime tidak tertinggal.

Pengujian end-to-end yang benar-benar memanggil Groq/MongoDB/Ubidots, mengunduh dependency baru, membuka kamera/mikrofon, dan memutar audio pada hardware target harus dilakukan pada mesin deployment karena membutuhkan API key, akses internet, permission browser, dan perangkat audio/kamera fisik. Gunakan `/api/health` setelah instalasi untuk melihat status service utama.

## Troubleshooting: MongoDB `TLSV1_ALERT_INTERNAL_ERROR`

Jika console menampilkan error seperti:

```text
SSL handshake failed ... [SSL: TLSV1_ALERT_INTERNAL_ERROR]
TopologyDescription ... ReplicaSetNoPrimary
```

lakukan pemeriksaan berikut:

1. Buka **MongoDB Atlas → Security → Network Access** lalu tambahkan **public IP** jaringan/komputer yang menjalankan Python server. IP seperti `192.168.x.x` adalah IP LAN dan bukan IP yang digunakan Atlas untuk allowlist.
2. Pastikan port outbound **TCP 27017** tidak diblokir firewall, VPN, antivirus HTTPS/TLS inspection, jaringan kampus/kantor, atau ISP.
3. Pastikan database user di **Atlas → Database Access** masih aktif dan URI berasal dari menu **Connect → Drivers**.
4. Jika password MongoDB mengandung karakter khusus seperti `@`, `:`, `/`, `?`, `#`, `[`, atau `]`, URL-encode password sebelum memasukkannya ke URI.
5. Update dependency project dengan `pip install -r requirements.txt`. Project menggunakan `certifi` sebagai CA bundle dan tetap melakukan validasi sertifikat TLS.
6. Jalankan diagnosis terpisah:

```bash
python tools/check_mongodb.py
```

Jika tes masih gagal, coba jalankan dari jaringan lain (misalnya hotspot). Jika langsung berhasil, masalah berada pada firewall/VPN/ISP/network access, bukan pada pipeline Groq/kamera.

> Jangan mengaktifkan `tlsInsecure=true` atau `tlsAllowInvalidCertificates=true` sebagai solusi permanen.

### Perilaku retry MongoDB

Koneksi MongoDB sekarang menggunakan exponential backoff. Jika Atlas sedang tidak dapat dijangkau, request AI/kamera tidak akan mencoba handshake MongoDB berulang kali. Retry dilakukan dari background task dengan interval awal `MONGODB_RETRY_INITIAL_SECONDS` dan meningkat sampai `MONGODB_RETRY_MAX_SECONDS`. Groq, kamera, TTS, dan Ubidots tetap berjalan meskipun MongoDB sedang offline.


## Diagnostik object detection

Jika OCR berjalan tetapi YOLO selalu menjawab tidak ada objek, jalankan:

```bash
python tools/check_object_detection.py
```

Untuk menguji foto Anda sendiri:

```bash
python tools/check_object_detection.py --image path\ke\foto.jpg
```

Pada server multi-device, log object detection sekarang menampilkan `count`, `pass`, jumlah kandidat mentah primary/fallback/center-crop, dan ukuran frame. Endpoint `/api/health` juga menampilkan `object_detection_error` jika model gagal diinisialisasi. Jika perlu menyimpan frame yang tetap kosong untuk debugging, set `YOLO_DEBUG_SAVE_EMPTY=true`; frame akan disimpan di `captured_images/yolo_empty/`. Matikan kembali setelah debugging karena snapshot kamera dapat mengandung informasi pribadi.
