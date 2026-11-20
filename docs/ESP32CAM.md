# ESP32-CAM: perakitan, flashing, konfigurasi

Firmware aktif: `firmware/esp32_cam/main.py` (**MicroPython**, dipindahkan dari
`iot tunanetra (2)/esp32Cam.py` — sudah teruji jalan lewat Thonny).
Sketsa Arduino lama di `firmware/esp32_cam/EXCEPT/esp32_cam.ino` disimpan
sebagai referensi tapi **tidak dipakai** (lihat `firmware/esp32_cam/README.md`
untuk alasannya).

## Perangkat & daya
- Modul **AI-Thinker ESP32-CAM** (OV2640, PSRAM 4 MB).
- Catu **5 V ≥ 1 A** ke pin 5V/GND. Reset acak/"Brownout detector" hampir selalu karena daya USB lemah.
- Untuk flashing: adaptor **ESP32-CAM-MB** atau USB-serial 3,3 V; hubungkan **GPIO0 → GND** saat mengunggah, lepas lalu reset.

## Flashing & instalasi (MicroPython + Thonny)
1. Flash firmware dasar MicroPython sekali saja:
   ```bash
   pip install esptool
   esptool.py --chip esp32 --port <PORT> erase_flash
   esptool.py --chip esp32 --port <PORT> --baud 460800 write_flash -z 0x1000 \
       firmware/esp32_cam/micropython_v1.21.0_camera_no_ble.bin
   ```
2. Buka board di **Thonny** (interpreter "MicroPython (ESP32)").
3. Edit `ssid` / `password` di `firmware/esp32_cam/main.py` agar sama dengan jaringan WiFi laptop (backend).
4. Simpan sebagai `main.py` di board (auto-run tiap boot) lalu reset.
5. Catat IP yang tercetak di konsol Thonny, mis. `Terhubung ke Wi-Fi. IP Address: 192.168.1.60`.
6. Isi `.env` backend:
   ```
   CAMERA_SOURCE=esp32cam
   ESP32CAM_URL=http://192.168.1.60
   ```
   `ESP32CAM_TOKEN` dan `ESP32CAM_FLASH` boleh dikosongkan — firmware ini tidak mendukung keduanya.

## Uji
```bash
python tools/check_esp32cam.py http://<ip> --frames 3 --save
# lihat captured_images/esp32cam_check.jpg untuk memeriksa sudut pandang & fokus
```
Langsung dari browser/curl: `http://<ip>/` (JPEG langsung — firmware ini membalas
*semua* path dengan foto baru, jadi `/status` atau `/capture` juga akan menghasilkan JPEG,
bukan JSON).

## Penyetelan
| Kebutuhan | Ubah (di `firmware/esp32_cam/main.py`) |
|---|---|
| Rambu jauh kurang terbaca | `camera.framesize(camera.FRAME_SXGA)` atau `FRAME_UXGA` (JPEG lebih besar, lebih lambat) |
| Terlalu lambat | `camera.framesize(camera.FRAME_SVGA)`, atau `camera.quality(12)` |
| Gambar terbalik/cermin | ubah `camera.flip(...)` / `camera.mirror(...)` |
| Fokus buram | putar cincin lensa OV2640 perlahan |

## Masalah umum
| Gejala | Penyebab/solusi |
|---|---|
| "Kamera tidak terhubung" diucapkan | IP salah/berubah, laptop & ESP32-CAM beda jaringan WiFi, ESP32-CAM belum di-reset setelah ganti kode |
| Reset berulang, "Brownout" | catu daya kurang |
| `camera init failed` di konsol Thonny | kabel pita tidak rapat, atau board bukan AI-Thinker (pin berbeda) |
| Gambar berpita/rusak | daya/kabel; turunkan resolusi |
| Server tidak merespons | pastikan `main.py` masih berjalan (cek konsol Thonny); firmware ini satu-thread, jadi satu koneksi diproses sepenuhnya sebelum menerima koneksi berikutnya |
