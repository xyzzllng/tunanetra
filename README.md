# Spider Sense — alat bantu tunanetra berbasis ESP32

Tekan tombol fisik → **ESP32-CAM** memotret → **backend** menjalankan AI → hasilnya
**diucapkan di speaker ESP32**. Tidak perlu webcam, browser, atau halaman web.

```
Tombol fisik ──► ESP32 utama ──WebSocket──► Backend Python ──HTTP GET /capture──► ESP32-CAM
                     ▲                          │  YOLOv5n · ekspresi · rambu · SegFormer
                     │   tts_ready (URL MP3)    │  (+ Groq, MongoDB, Ubidots: opsional)
                     └──────────────────────────┘  TTS Edge/gTTS → MP3
   Speaker I2S ◄── ESP32 mengunduh & memutar MP3
```

| Tombol | Mode | Yang diucapkan |
|---|---|---|
| 1 | Deteksi Objek (burst 3 frame + SegFormer + sensor jarak) | objek/area, posisi, kemungkinan halangan |
| 2 | Deteksi Ekspresi (burst 3 frame) | netral / senang / sedih, atau "belum terlihat jelas" |
| 3 | Deteksi Rambu Lalu Lintas (YOLO custom) | rambu/lampu lalu lintas yang terbaca |

> **Batas penting.** Ini prototipe. Jangkauan 50 cm tidak dapat dijamin oleh perangkat lunak,
> kamera monokular tidak mengukur jarak absolut, dan hasil kosong bukan berarti jalur aman.
> Jangan jadikan satu-satunya alat navigasi. Server belum punya autentikasi/HTTPS: jalankan hanya
> di jaringan tepercaya.

## Struktur

```
main.py                 titik masuk:  python main.py
config/settings.py      semua pengaturan (.env)
ai/                     logika deteksi (input: frame BGR; tidak tahu kamera/HTTP)
camera/                 sumber gambar: esp32cam.py (utama), webcam.py (opsional, uji meja)
devices/                komunikasi ESP32: ws.py (WebSocket), events.py (hub tombol), serial_bridge.py
services/               quick_commands (orkestrasi), sessions, audio+speech (STT/TTS), database
server/                 app.py (perakitan Flask), upload.py (tanya-jawab suara/teks)
firmware/               esp32_cam (MicroPython, aktif) · esp32_main (tombol+ultrasonik+speaker) · esp32_multidevice (lama)
model/                  bobot YOLOv5n, CNN ekspresi, YOLO rambu, source yolov5
web/                    dashboard monitor OPSIONAL (WEB_UI_ENABLED=true); bukan dependency
tools/  tests/  docs/
```

## Menjalankan

1. **Backend** (Python 3.11/3.12):
   ```bash
   python -m venv .venv 
   .venv\Scripts\activate
   pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   pip install -r requirements.txt
   cp .env.example .env          # isi ESP32CAM_URL, kunci Groq/Mongo/Ubidots bila dipakai
   python tools/prepare_models.py   # sekali: unduh SegFormer (tembok/pintu)
   python tools/check_esp32cam.py http://<ip-kamera> --save   # uji kamera tanpa backend
   python main.py
   ```
2. **Firmware**:
   - ESP32-CAM: `firmware/esp32_cam/main.py` (MicroPython, via Thonny) — lihat `docs/ESP32CAM.md`
     atau `firmware/esp32_cam/README.md`.
   - ESP32 utama (tombol/ultrasonik/speaker): `firmware/esp32_main/` (Arduino; salin
     `secrets.h.example` → `secrets.h`; `SERVER_HOST` = IP komputer backend).
3. **Uji tanpa perangkat keras tombol**: `python tools/check_esp32_button.py --host <ip> --button 1`.

Tes: `pytest -q` (Node ≥ 18 untuk `node tests/test_frontend.cjs`). Tes yang memuat model asli
membutuhkan `torch` terpasang.

## Dokumentasi

- `docs/ARSITEKTUR.md` — apa berjalan di mana, protokol, batasan hardware
- `docs/ESP32CAM.md` — perakitan, flashing, konfigurasi, pemecahan masalah kamera
- `docs/MIGRASI.md` — pemetaan file/fungsi lama → baru, yang dihapus, dependency
- `docs/AI_DETAIL.md` — perilaku tiga mode AI, ambang, confidence
- `docs/archive/` — dokumen sejarah (path lama)

Dashboard web di `web/` menampilkan status koneksi aktual, informasi perangkat,
alur kerja, dan simulasi demonstrasi. Perintah deteksi manual menggunakan
ESP32-CAM yang terhubung ke backend; halaman web tidak meminta akses kamera
browser dan tidak mengirim gambar dari kamera HP. Status ESP32 Main berasal dari
registrasi WebSocket aktif, sedangkan status ESP32-CAM diperiksa melalui probe
JPEG backend.
