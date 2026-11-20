> **Catatan migrasi:** dokumen ini mempertahankan uraian perilaku AI (tiga mode, ambang,
> confidence, tembok/pintu). Bagian *instalasi/menjalankan*, `SERVER_MODE`, webcam browser,
> dan EasyOCR di bawah ini **sudah usang**. Untuk arsitektur dan cara menjalankan yang
> berlaku, lihat `README.md`, `docs/ARSITEKTUR.md`, dan `docs/MIGRASI.md`.
> Deteksi "tulisan" kini adalah deteksi rambu lalu lintas (`ai/sign_detection.py`, YOLO).

> Pembaruan AI: lihat [laporan perubahan dan validasi](docs/PERUBAHAN_AI.md).
> Ekspresi berfokus pada netral/senang/sedih, OCR mempertahankan gambar asli,
> dan jawaban speaker dibuat singkat. Tiga tombol layar tersedia di panel
> **Pengaturan & kontrol**. Kunci `.env` lama tetap digunakan.

# Spider Sense — 3 Quick Command

Penyempurnaan proyek `camdetection_latest`, bukan proyek baru. YOLOv5n, CNN
ekspresi dengan 7 logit internal (output netral/senang/sedih), Haar frontal-face, dan EasyOCR tetap dipakai. Endpoint lama,
Groq/Whisper, Edge TTS/gTTS, MongoDB, Ubidots, dan pairing ESP32 dipertahankan.

**Batas penting:** jangkauan 50 cm tidak bisa dijamin lewat software. Webcam
monokular tidak mengukur jarak absolut otomatis. Uji kamera nyata terlebih
dahulu; hasil kosong bukan berarti jalur aman. Jangan jadikan prototipe ini
satu-satunya alat bantu navigasi atau pengganti tongkat/sensor.

## Instalasi dan menjalankan

Gunakan Python 3.11 atau 3.12. Windows PowerShell, dari folder hasil ekstrak:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
# CPU; lewati jika sudah memasang pasangan torch/torchvision CUDA yang sesuai.
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
Copy-Item .env.example .env
python tools/prepare_models.py
python main.py
```

Jangan menimpa `.env` lama berisi kredensial: gabungkan parameter baru secara
manual. YOLO dan model ekspresi sudah dalam ZIP. EasyOCR (sekitar 95 MiB) dan
SegFormer (sekitar 15 MiB) diunduh sekali lewat `prepare_models.py`, bukan saat
tombol ditekan. Untuk hanya satu model gunakan `--ocr` atau `--scene`.

Linux: buat venv dengan `python3 -m venv .venv`, aktifkan dengan
`source .venv/bin/activate`, lalu perintah pip/Python yang sama. Salin contoh
dengan `cp .env.example .env` hanya jika `.env` belum ada.

Buka **http://localhost:5000** di komputer yang sama. Default
`SERVER_MODE=multi`: webcam dimiliki browser, bukan Python. Pilih kamera di
dropdown; pilih kamera depan saat menguji ekspresi jika perlu.

HP/perangkat lain memerlukan HTTPS untuk kamera browser. HTTP alamat IP LAN
biasa tidak cukup. Server ini masih prototipe tanpa autentikasi; jangan
mengekspos langsung ke internet. Terapkan HTTPS, autentikasi dan pembatasan
akses sebelum digunakan di luar jaringan tepercaya.

Untuk webcam yang dibuka Python, ubah `.env`, kemudian restart:

```dotenv
SERVER_MODE=single
WEBCAM_SOURCE=0
LOCAL_AUDIO_PLAYBACK=true
```

Halaman yang sama menampilkan tiga tombol. Browser tidak memanggil webcam
sendiri; preview dan perintah berbagi satu handle kamera server yang dikunci.
Jangan menjalankan kedua server sekaligus untuk webcam fisik yang sama.
Preview membaca frame saat dibuka tetapi tidak menjalankan AI.

## Perilaku tiga tombol

| Tombol | Proses | Hasil |
|---|---|---|
| Deteksi Objek | Burst 3 frame → YOLO → konfirmasi antargambar; SegFormer sekali di frame akhir | Objek/area, posisi, kemungkinan halangan; skor hanya metadata |
| Deteksi Ekspresi | Burst 3 frame → Haar → wajah tengah → CNN/crop → konfirmasi | Netral/senang/sedih, atau “belum terlihat jelas” |
| Deteksi Rambu Lalu Lintas | Snapshot → YOLO rambu (`model/rambu_yolo.pt`, 40 kelas) → warna lampu lalu lintas | Nama rambu yang terdeteksi; label/kotak pada API (`sign_*`) |

Mode tidak membuka ulang webcam. Satu pekerjaan vision aktif per proses;
request bersamaan mendapat busy/429, bukan antrean tak terbatas. TTS/pencatatan
berada dalam antrean background terbatas, tidak menunda hasil visual.
Request ID sama tidak menjalankan ulang AI; klik baru menghasilkan ID baru,
sehingga tidak mengambil hasil basi hanya karena tombol yang sama ditekan.

Tiga tombol tidak memanggil Groq untuk mengarang jawaban; hasil langsung dari
detektor/OCR. Kontrol suara lama ada di panel opsional, termasuk push-to-talk
ESP32. Groq API key hanya diperlukan untuk percakapan/STT. TTS online tetap
memerlukan internet; speech synthesis browser menjadi fallback bila MP3 gagal.
Kotak hasil Quick Command ada pada **snapshot yang dianalisis**, bukan overlay
basi di video bergerak. Ini bukan peringatan navigasi realtime.

## Tembok/pintu dan model tambahan

COCO pada YOLOv5n bawaan tidak memiliki kelas `wall`/`door`. Mengubah threshold
tidak menambahkan kelas tersebut. `ai/scene.py` menambahkan
`nvidia/segformer-b0-finetuned-ade-512-512` melalui `transformers`,
`huggingface-hub`, dan `safetensors`. B0 dipilih untuk menekan beban komputasi,
tetapi tetap menambah RAM dan latency pada mode objek.

Segmentasi memakai confidence piksel, luas area, serta perpotongan mask dengan
koridor tengah bawah; tidak hanya bounding box yang bisa mengelilingi pintu
kosong. Pintu terdeteksi belum tentu tertutup. Tembok jauh juga belum tentu
bahaya dekat. Objek/area diprioritaskan sebagai **kemungkinan** penghalang.

**Lisensi SegFormer yang dirujuk dibatasi untuk penelitian/evaluasi
nonkomersial.** Untuk produk komersial, peroleh hak yang sesuai atau ganti
model sebelum deployment. Source YOLOv5 memiliki lisensi AGPL-3.0 pada paket
bawaan; asal dataset/lisensi model ekspresi pengguna belum disertakan.

Sumber: [model card](https://huggingface.co/nvidia/segformer-b0-finetuned-ade-512-512),
[kelas model](https://huggingface.co/nvidia/segformer-b0-finetuned-ade-512-512/blob/main/config.json),
[lisensi NVIDIA](https://github.com/NVlabs/SegFormer/blob/master/LICENSE),
[API EasyOCR](https://www.jaided.ai/easyocr/documentation/).

Bila SegFormer belum siap, YOLO tetap dicoba dan peringatan tembok/pintu tidak
aktif ditampilkan. `SCENE_SEGMENTATION=false` mematikannya dengan konsekuensi
hilangnya cakupan tersebut.

## Confidence yang jujur

- YOLO: skor model sesudah NMS/filter, bukan akurasi dunia nyata. Burst memakai
  median skor asli dengan pencocokan label/IoU; skor tidak ditambah buatan.
- SegFormer: mean probabilitas piksel pada area. Tidak langsung sebanding
  dengan skor YOLO.
- Ekspresi: softmax CNN belum dikalibrasi dengan validation set. Threshold skor
  dan selisih dua kelas terbaik dipakai. Hanya netral/senang/sedih yang
  ditampilkan; tujuh logit tetap diperlukan checkpoint. Ekspresi visual bukan kepastian emosi.
- Haar tidak memberi probabilitas wajah terkalibrasi; `face_confidence=null`.
  Parameter `FACE_MIN_NEIGHBORS` adalah filter, bukan persentase keyakinan.
- Kiri/tengah/kanan merupakan posisi dalam gambar, bukan pemetaan ruang 3D.

## Jarak 50 cm dan kalibrasi

`OBSTACLE_WARNING_CM=50` adalah threshold perhatian sensor, **bukan** pengubah
jangkauan kamera atau penyaring objek. Objek <50 cm tetap diproses, begitu pula
objek pada 50 cm dan lebih jauh sesuai visibilitas dan kemampuan model.

Ultrasonik dikaitkan ke sesi ESP32, kedaluwarsa setelah 4 detik, menolak nilai
nonangka/NaN/di luar rentang 2–500 cm. Median singkat meredam noise, tetapi nilai
terbaru yang lebih dekat langsung dipakai. Jarak sensor tidak ditempelkan ke
semua objek YOLO karena bidang pandangnya berbeda. Firmware tetap menangani
peringatan lokal <50 cm, tidak diubah, dan tidak mengirim jarak terus-menerus
di luar rentang peringatan aslinya. Sampel lama ditampilkan tidak tersedia.

Default `DISTANCE_CALIBRATION_FILE` kosong: tidak ada sentimeter kamera palsu.
Untuk **satu objek dengan lebar diketahui**, model pinhole opsional memakai:

`focal_px = box_width_px × measured_distance_cm / real_width_cm`

`estimated_distance_cm = real_width_cm × scaled_focal_px / detected_width_px`

Contoh berikut hanya demonstrasi; ganti dengan hasil meteran/piksel Anda:

```bash
python tools/calibrate_distance.py --camera-id server --label bottle --width-cm 10 --distance-cm 50 --box-width-px 160 --image-width 1280 --image-height 960
```

Salin JSON output ke `calibration.json`; isi
`DISTANCE_CALIBRATION_FILE=calibration.json`. Kamera browser memakai `deviceId`
aktual, terlihat pada form `camera_id` di Developer Tools → Network.

Kalibrasi berlaku hanya untuk camera_id, label, dan aspect ratio yang cocok.
Kotak terpotong ditolak; lebar piksel dalam burst dimedian-kan. Ulangi
kalibrasi jika zoom/fokus/FOV/crop atau objek berubah. Botol berbeda ukuran
tidak boleh dianggap sama hanya karena labelnya sama. Jangan memakai ukuran
tebakan untuk manusia, tembok, atau pintu yang hanya tampak sebagian.

## Konfigurasi `.env`

Restart setelah perubahan. Jika memakai `.env` lama, set threshold YOLO ke
default baru; fallback sekarang dijepit minimal sebesar threshold utama.

| Parameter | Default | Fungsi |
|---|---:|---|
| `YOLO_CONFIDENCE` | 0.40 | Filter skor objek |
| `YOLO_SMALL_OBJECT_CONFIDENCE` | 0.50 | Filter objek sangat kecil dalam gambar |
| `YOLO_IOU` | 0.45 | NMS kotak duplikat |
| `YOLO_IMAGE_SIZE` | 640 | Resolusi input YOLO |
| `YOLO_FALLBACK_IMAGE_SIZE` | 768 | Input recovery saat hasil kosong |
| `YOLO_MODEL_PATH` | model/yolov5n.pt | Bobot YOLOv5 custom tepercaya juga bisa dipakai |
| `QUICK_OBJECT_FRAMES` | 3 | 1–3 frame; 1 tidak memiliki konfirmasi temporal |
| `AI_CPU_THREADS` | 2 | Thread intra-op PyTorch |
| `WEBCAM_WIDTH`, `WEBCAM_HEIGHT` | 1280, 720 | Resolusi yang diminta; kamera boleh memilih lainnya |
| `WEBCAM_SOURCE` | 0 | Index webcam server single |
| `OBSTACLE_WARNING_CM` | 50 | Threshold perhatian sensor, bukan jangkauan optik |
| `OBSTACLE_LARGE_AREA` | 0.08 | Rasio luas untuk memperhatikan objek besar lain |
| `SCENE_CONFIDENCE` | 0.65 | Ambang piksel segmentasi |
| `SCENE_MIN_AREA_RATIO` | 0.015 | Filter fragmen area kecil |
| `FACE_CENTER_WIDTH`, `FACE_CENTER_HEIGHT` | 0.40, 0.70 | Area tengah untuk pusat wajah |
| `FACE_MIN_WIDTH_RATIO` | 0.10 | Lebar minimum wajah relatif ke gambar |
| `FACE_MIN_NEIGHBORS` | 6 | Filter Haar |
| `EMOTION_MIN_CONFIDENCE` | 0.55 | Ambang netral; senang/sedih memakai minimal 0.70 |
| `QUICK_EXPRESSION_FRAMES` | 3 | Burst ekspresi 1–3 frame |
| `EMOTION_MIN_MARGIN` | 0.15 | Selisih dua kelas ekspresi terbaik |
| `SIGN_MIN_CONFIDENCE` | 0.35 | Ambang minimum deteksi rambu |
| `SIGN_RELIABLE_CONFIDENCE` | 0.50 | Rata-rata confidence agar hasil rambu diucapkan |
| `SIGN_IMAGE_SIZE` | 640 | Ukuran input YOLO rambu |
| `SIGN_MODEL_PATH` | model/rambu_yolo.pt | Lokasi model rambu |
| `SAVE_CAPTURES` | false | Simpan foto pada jalur kamera legacy bila diperlukan |

YOLO, segmentasi, ekspresi memakai CPU default. Ubah `YOLO_DEVICE`,
`SCENE_DEVICE`, `EMOTION_DEVICE` hanya jika CUDA dan pasangan
torch/torchvision sesuai. Model dimuat sekali sesuai kebutuhan, dibagi semua
sesi, tidak dimuat ulang setiap klik. Untuk mengurangi beban coba
`QUICK_OBJECT_FRAMES=2`; mengurangi resolusi/mematikan segmentasi mempunyai
trade-off recall/cakupan.

## Pengujian

```bash
pip install pytest
python -m pytest
node tests/test_frontend.cjs
node tests/test_esp32_frontend.cjs
python tools/audit_project.py
python tools/check_esp32_button.py --status
python tools/check_object_detection.py
python tools/check_quick_commands.py --repeat 2
python tools/check_object_detection.py --image path/ke/foto.jpg
```

Node hanya untuk tes JavaScript. `GET /api/health` dan `/api/client_config`
menampilkan state model `not_loaded`, `ready`, `error`. `not_loaded` sebelum
klik pertama adalah normal. Kegagalan inisialisasi dapat dicoba kembali
setelah 30 detik tanpa mematikan mode lain. Batas pengujian ada di
`docs/VALIDASI.md`.

### Uji lapangan

1. Sambil diam, dengan pendamping dan meteran, uji tembok, pintu terbuka/tertutup,
   meja, kursi, manusia, serta objek kecil pada 30, **50**, 75, 100 cm.
2. Minimal 20 klik per kondisi: terang/redup, tengah/samping, tertutup sebagian,
   frame kosong. Catat benar/salah/terlewat dan latency.
3. Bandingkan versi lama pada kumpulan gambar sama; ukur precision/recall per
   kelas dan false alert, bukan rata-rata confidence. Tidak ada dataset
   validation berlabel dalam input untuk mengklaim peningkatan akurasi.
4. Bandingkan estimasi jarak dengan meteran pada jarak lain di luar kalibrasi.
5. Uji ganti tiga tombol, cabut/pasang kamera, model hilang, koneksi putus,
   dua sesi serentak, dan pairing ulang ESP32.

## Tombol fisik ESP32 (remote controller)

Tiga tombol fisik memicu tombol layar yang sama, tanpa menonaktifkan mouse/touch:
tombol 1 Deteksi Objek, tombol 2 Deteksi Ekspresi, tombol 3 Deteksi Rambu Lalu Lintas.
ESP32 hanya mengirim event; deteksi tetap dijalankan Python/browser lewat
`/api/quick` yang sudah ada.

ESP32 mengirim event lewat WebSocket `/ws` (`type=quick_command`) atau
`POST /api/esp32/event`. Server meneruskannya ke halaman web lewat SSE
`GET /api/esp32/stream`, dan halaman memanggil `runQuick()` - fungsi yang sama
dengan handler klik tombol UI. Bila ESP32 mati atau stream putus, tombol layar
tetap berfungsi normal.

Firmware: `esp32_quickbutton/esp32_quickbutton.ino` (3 tombol + beep, Wi-Fi,
reconnect otomatis). Tombol pada GPIO 4/18/19, `INPUT_PULLUP` aktif LOW ke GND.
Firmware lengkap `esp32_multidevice.ino` juga menerima tiga tombol yang sama,
tetapi pada GPIO 32/33/27 karena 4/18/19 sudah dipakai push-to-talk, TRIG, dan
toggle di sana.

Uji tanpa perangkat keras: `python tools/check_esp32_button.py --status`, lalu
`--button 1`, `--button 2`, `--button 3`. Status jalur: `GET /api/esp32/status`.
Panduan lengkap wiring, konfigurasi `.env`, dan pengujian ada di
`docs/ESP32_QUICK_BUTTON.md`.

## Struktur dan kompatibilitas

`ai/pipeline.py`: mode; `ai/spatial.py`: prioritas/jarak; `ai/scene.py`:
segmentasi; `services/quick_commands.py`: API bersama. Modul asli
`deteksiObjek.py`, `deteksiWajah.py`, `bacaTex.py` diperbaiki. `main.py` tetap
entry point; dependency lama tidak dihapus.

`POST /api/quick`: multipart `session_id`, `request_id`,
`mode=objek|ekspresi|baca`, `image` berulang untuk burst browser. Mode server
mengambil frame sendiri. `/api/upload`, `/ws`, MP3, follow-up, hitung wajah,
dan endpoint single legacy tetap ada. Root single JSON lama tersedia di
`/api/legacy_status`; `/` kini halaman tombol. `mode=wajah` di upload multi
kini berarti ekspresi; gunakan `hitung_wajah` untuk hitungan tanpa CNN.

Jalankan **satu proses server**: session/pairing/audio-job masih in-memory,
belum cocok untuk multi-worker tanpa shared store. Dokumentasi integrasi
awal tersimpan pada `docs/README_LEGACY.md`.
