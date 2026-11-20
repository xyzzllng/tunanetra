# Arsitektur

## 1. Sebelum vs sesudah

**Sebelum** — browser adalah perantara wajib (kamera dan pemicu):

```
Tombol ESP32 → WS /ws → hub → SSE → BROWSER → runQuick() → getUserMedia (webcam)
      → POST /api/quick → Python (AI) → MP3 → tts_ready → ESP32 → speaker
```
Tanpa tab browser terbuka, tombol fisik tidak melakukan apa pun.

**Sesudah** — backend memicu kamera sendiri:

```
Tombol ESP32 → WS /ws → ESP32EventHub.dispatch → QuickCommandService.start_from_button
      → GET http://<esp32-cam>/ (×3) → AI (ai/pipeline.py) → TTS MP3
      → {"type":"tts_ready","url":...} → ESP32 mengunduh MP3 → speaker I2S
```

## 2. Apa berjalan di mana

| Komponen | Tugas | Alasan |
|---|---|---|
| **ESP32-CAM** (`firmware/esp32_cam/main.py`, MicroPython) | server TCP sangat sederhana: request apa pun ke port 80 dibalas satu JPEG segar (tanpa path/token) | RAM ≈ 520 KB + PSRAM 4 MB, CPU 240 MHz: tidak realistis untuk YOLOv5n, CNN, SegFormer (puluhan–ratusan MB, PyTorch) |
| **ESP32 utama** (`firmware/esp32_main`) | 3 tombol, ultrasonik HC-SR04, buzzer, decode MP3 → speaker I2S | I/O waktu-nyata; peringatan halangan dekat tetap lokal (tanpa jaringan) |
| **Backend Python** (`main.py`) | orkestrasi, YOLOv5n, ekspresi, rambu, SegFormer, sensor-fusion jarak, TTS, sesi | butuh CPU/RAM sebuah komputer |
| **Cloud/API** | Groq (STT + tanya-jawab), MongoDB Atlas, Ubidots, Edge TTS/gTTS | layanan eksternal; semuanya gagal-aman |

Backend bisa berupa laptop, mini-PC, Raspberry Pi 5 (uji dulu kecepatan), atau VPS **bila** ESP32-CAM
terjangkau dari backend (satu LAN/VPN). Backend menarik gambar (*pull*), sehingga ESP32-CAM di belakang NAT
tidak dapat dijangkau VPS publik tanpa VPN/tunnel.

## 3. Protokol

ESP32 utama → backend (WebSocket `/ws`, JSON):

| Pesan | Arti |
|---|---|
| `{"type":"register","role":"esp32","device_id":"esp32-A1"}` | daftar; backend membuat sesi otomatis `dev-esp32-A1` |
| `{"type":"quick_command","event":"object_detection","button":1}` | tombol 1/2/3 (`object_detection`, `expression_detection`, `sign_detection`; nama lama `text_detection` masih diterima sebagai alias dan menjalankan deteksi rambu) |
| `{"type":"quick_command","event":"obstacle_warning","distance_cm":42}` | sampel jarak ultrasonik (dipakai ringkasan objek; tidak dibalas) |
| `{"type":"ultrasonic","distance_cm":..}` | sampel jarak (firmware `esp32_multidevice`; boleh memicu ucapan info) |

Backend → ESP32:

| Pesan | Arti / reaksi firmware |
|---|---|
| `quick_ack` `delivered:1` | perintah dimulai → beep konfirmasi tinggi |
| `quick_ack` `delivered:0` / `error` | ditolak (sibuk, cooldown) atau gagal → beep rendah |
| `tts_ready` `{url}` | ESP32 `GET`-kan MP3 dari `http://SERVER_HOST:SERVER_PORT<url>` lalu memutar |

Jalur alternatif pemicu: `POST /api/esp32/event {"button":1,"device_id":..}` (uji/curl) dan serial USB
(`ESP32_SERIAL_ENABLED=true`; suara lalu diputar di komputer backend karena serial tidak membawa audio).

Backend → ESP32-CAM: `GET http://<ip>/` (path/token diabaikan firmware — lihat
`firmware/esp32_cam/README.md`). Burst = 3 permintaan berurutan (jeda `ESP32CAM_BURST_GAP_MS`);
resolusi harus sama antar frame.

## 4. Ketahanan (kegagalan tidak boleh senyap bagi pengguna tunanetra)

| Kegagalan | Perilaku |
|---|---|
| ESP32-CAM mati/tidak terjangkau | retry singkat, lalu diucapkan "Kamera tidak terhubung…" di speaker ESP32; bila TTS juga gagal → beep rendah |
| Model AI gagal dimuat | diucapkan "<Mode> belum tersedia."; perintah berikutnya tidak terkunci |
| Tombol ditekan saat proses lain berjalan | ditolak segera (beep rendah), tidak diantre |
| MongoDB/Ubidots/Groq mati | hasil visual dan suara tetap keluar; pencatatan dilewati |
| Internet mati | deteksi tetap jalan tetapi **TTS online tidak tersedia** → hanya beep (lihat batasan) |
| ESP32-CAM macet | firmware restart sendiri setelah 5 kegagalan capture berturut-turut |

## 5. Batasan yang perlu diketahui

- **Suara membutuhkan internet** (Edge TTS → gTTS). TTS offline (mis. Piper) belum ada; ini titik lemah terbesar
  bila alat dipakai di luar jaringan Wi-Fi/internet.
- **Tanya-jawab suara** (`/api/upload` dengan audio) membutuhkan mikrofon; firmware belum punya mikrofon I2S.
  Endpoint teks (`transcript`) dan audio dari klien lain tetap bekerja. Push-to-talk `esp32_multidevice` hanya
  berguna dengan klien perekam.
- **Kalibrasi jarak lama tidak berlaku** untuk ESP32-CAM (OV2640, sudut pandang berbeda). Ulangi dengan
  `tools/calibrate_distance.py --camera-id esp32cam`. Tanpa kalibrasi, sistem tidak memberi angka cm dari kamera.
- Latensi: 3 frame XGA lewat Wi-Fi ≈ 0,5–1,5 dtk, ditambah inferensi CPU dan TTS. Ukur di lapangan.
- Tanpa autentikasi/HTTPS pada backend maupun ESP32-CAM (firmware MicroPython ini tidak mendukung token);
  pakai `ESP32_SHARED_TOKEN` untuk jalur ESP32 utama minimal, dan jaringan tepercaya untuk ESP32-CAM.

## 6. Dashboard web opsional

`web/` adalah dashboard monitor dan demonstrasi, bukan perantara untuk fungsi
perangkat. Dashboard tidak meminta izin kamera browser. Perintah deteksi manual
memanggil `/api/quick` tanpa gambar klien sehingga backend tetap mengambil
gambar dari ESP32-CAM; pairing WebSocket dan monitor event SSE tetap tersedia.
Status ESP32 Main mengikuti registrasi socket yang masih aktif, status
ESP32-CAM mengikuti probe JPEG, dan data jarak hanya ditampilkan selama sampel
ultrasonik masih segar (maksimal empat detik).
