# Catatan penggabungan — SPIDER-SENSE + iot tunanetra (2)

Project ini adalah hasil gabungan dua folder yang diunggah:

- **SPIDER-SENSE.zip** — arsitektur backend penuh (tombol fisik → WebSocket →
  AI → TTS → speaker ESP32), dipakai sebagai **dasar/base** project ini.
- **iot tunanetra (2).zip** — sumber untuk **firmware ESP32-CAM (MicroPython)**
  dan **kode Flask yang mengambil gambar dari kamera**.

## Apa yang diambil dari mana

| Bagian | Sumber | Keterangan |
|---|---|---|
| Deteksi YOLO (objek, rambu lalu lintas), ekspresi, segmentasi | **SPIDER-SENSE** (`ai/`) | tidak diubah sama sekali |
| Orkestrasi tombol, WebSocket ESP32, sesi, TTS, database | **SPIDER-SENSE** (`devices/`, `services/`, `server/`, `config/`) | tidak diubah |
| Firmware ESP32 utama (tombol+ultrasonik+speaker) | **SPIDER-SENSE** (`firmware/esp32_main/`) | tidak diubah — tidak diminta |
| **Firmware ESP32-CAM** | **iot tunanetra (2)** (`esp32Cam.py`) | dipindah ke `firmware/esp32_cam/main.py` (MicroPython) |
| **Client kamera di backend Flask** | diadaptasi dari **iot tunanetra (2)** (`utils/camera.py`) | ditulis ulang jadi `camera/esp32cam.py`, tetap memenuhi antarmuka yang dipakai `services/quick_commands` |

## Kenapa firmware ESP32-CAM diganti ke MicroPython?

SPIDER-SENSE aslinya menyediakan sketsa Arduino (`firmware/esp32_cam/EXCEPT/esp32_cam.ino`)
dengan endpoint `/capture` + `/status` dan dukungan token. Tapi dokumentasi lamanya
sendiri mencatat sketsa ini **belum pernah diuji di perangkat keras nyata**.
Firmware MicroPython dari iot tunanetra (2) sebaliknya sudah terbukti jalan
(dijalankan lewat Thonny). Karena itu, di project gabungan ini:

- Firmware aktif = `firmware/esp32_cam/main.py` (MicroPython).
- Sketsa Arduino lama tetap disimpan di `firmware/esp32_cam/EXCEPT/` sebagai referensi,
  tapi **tidak dipakai**.

## Perbedaan penting firmware baru vs lama

Firmware MicroPython ini **tidak membaca path/method** dari request HTTP sama
sekali — koneksi apa pun ke port 80 langsung dibalas satu JPEG segar. Karena itu:

- Tidak ada endpoint `/capture` atau `/status` terpisah.
- Tidak ada pemeriksaan token (`ESP32CAM_TOKEN` di `.env` diabaikan, hanya dicatat di log).
- Tidak ada kontrol LED flash dari backend (`ESP32CAM_FLASH` diabaikan).

`camera/esp32cam.py` (backend) dan dokumentasi (`docs/ESP32CAM.md`,
`docs/ARSITEKTUR.md`, `README.md`, `config/settings.py`) sudah disesuaikan
dengan perilaku ini. `docs/MIGRASI.md` (catatan migrasi historis SPIDER-SENSE)
dibiarkan apa adanya kecuali catatan tambahan di bagian atas, karena isinya
adalah arsip sejarah proyek sebelumnya.

## Yang TIDAK ikut digabung

- `Web.py` (dashboard Streamlit di iot tunanetra (2)) — tidak diminta, tidak
  disentuh, tidak ada di project ini.
- `mic.py` / `speaker.py` (firmware ESP32 utama versi iot tunanetra, berbasis
  polling HTTP `/upload` dan `/mp3`) — tidak diikutkan karena SPIDER-SENSE
  sudah punya arsitektur ESP32-utama-nya sendiri (tombol + WebSocket +
  push TTS) di `firmware/esp32_main/`, dan permintaan penggabungan hanya
  menyebut kamera + deteksi YOLO.
- `fix new - Copy/server.py` dan `bridge_server.py` (backend Flask versi lama
  di iot tunanetra (2)) — fungsinya sudah digantikan sepenuhnya oleh
  `server/app.py` + `services/quick_commands.py` milik SPIDER-SENSE, jadi
  tidak diikutkan supaya tidak ada dua backend yang tumpang tindih.

## Langkah berikutnya untuk mencoba project ini

1. `pip install -r requirements.txt` (Python 3.11/3.12; lihat `README.md` untuk torch).
2. Isi `.env` (contoh di `.env.example`) — `ESP32CAM_URL` harus IP ESP32-CAM Anda.
3. Flash + jalankan `firmware/esp32_cam/main.py` di ESP32-CAM lewat Thonny — lihat
   `firmware/esp32_cam/README.md`.
4. `python tools/check_esp32cam.py http://<ip-kamera> --save` untuk uji kamera saja.
5. `python main.py` untuk menjalankan backend penuh.
