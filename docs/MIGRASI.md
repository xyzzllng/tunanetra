# Migrasi: webcam/browser → ESP32-CAM/backend headless

> **Catatan (project gabungan ini):** dokumen di bawah adalah catatan migrasi historis dari arsip
> `tunanetra.zip` ke arsitektur SPIDER-SENSE. Setelah dokumen ini ditulis, firmware ESP32-CAM dan
> client Python-nya (`camera/esp32cam.py`) diganti sekali lagi — bukan lagi sketsa Arduino
> `esp32_cam.ino`, melainkan firmware **MicroPython** dari `iot tunanetra (2)/esp32Cam.py` (sudah
> teruji jalan). Lihat `firmware/esp32_cam/README.md` dan `docs/ESP32CAM.md` untuk perilaku yang
> berlaku sekarang; referensi `GET /capture` di bawah ini sudah tidak akurat (firmware sekarang
> membalas semua path dengan JPEG langsung, tanpa endpoint terpisah).

## Pembaruan dashboard web

Dashboard `web/` kini menjadi halaman presentasi dan monitor dengan status
koneksi, pengenalan proyek, komponen, alur kerja, dan area demonstrasi. Dashboard
tidak lagi mengambil gambar dari kamera browser: `/api/quick` dipanggil tanpa
file gambar agar backend menggunakan ESP32-CAM. Pairing melalui `/ws` dan
monitor event melalui `/api/esp32/stream` tetap dipertahankan. Status ESP32 Main
dan jarak ultrasonik memakai data perangkat aktif/terbaru dari backend; animasi
3D pada demo masih berupa placeholder.

Dokumen ini menjawab bagian A–G permintaan migrasi. Semua pernyataan berasal dari kode di arsip `tunanetra.zip`.

## 0. Temuan kunci

1. **`web/app.js` tidak berisi logika deteksi.** Ia hanya klien: mengambil frame webcam, `POST /api/quick`,
   menggambar kotak, merekam suara, memutar audio. Deteksi ada di `ai/*.py`, dipanggil lewat
   `services/quick_commands.py`. Karena itu tidak ada fungsi deteksi yang hilang dengan tidak memakai `web/`.
2. Browser adalah **perantara wajib** (kamera + pemicu): tombol ESP32 → SSE → browser → `runQuick()`.
3. Dukungan mode "webcam di server" sudah ada (`camera != None` di `quick_commands`) — dipakai sebagai titik sambung ESP32-CAM.

## A. Struktur project baru

```
spider-sense/
├── main.py                       titik masuk tunggal
├── README.md  requirements.txt  pytest.ini  .env.example  .gitignore
├── config/settings.py            + ESP32CAM_*, CAMERA_SOURCE, WEB_UI_ENABLED, HOST
├── ai/                           logika deteksi (tidak tahu kamera/HTTP)
│   ├── object_detection.py  expression_detection.py  sign_detection.py  scene_segmentation.py
│   ├── spatial.py  pipeline.py  runtime.py  output.py  llm.py
├── camera/
│   ├── __init__.py               CameraError + build_camera()
│   ├── esp32cam.py               BARU  sumber utama (pull HTTP)
│   └── webcam.py                 opsional (uji AI tanpa perangkat keras)
├── devices/
│   ├── ws.py                     WebSocket /ws (ESP32)
│   ├── events.py                 hub tombol + /api/esp32/*
│   └── serial_bridge.py          jalur USB opsional
├── services/
│   ├── quick_commands.py         QuickCommandService (tombol fisik + /api/quick)
│   ├── sessions.py  audio.py  speech.py  database.py
├── server/
│   ├── app.py                    perakitan Flask (menggantikan server.py + server_multidevice.py)
│   └── upload.py                 /api/upload (Groq), /api/audio_file, /mp3, /api/session
├── firmware/
│   ├── esp32_cam/                BARU  esp32_cam.ino + secrets.h.example
│   ├── esp32_main/               esp32_main.ino (isi tidak diubah)
│   └── esp32_multidevice/        firmware lama (opsional)
├── model/                        bobot + yolov5 lokal (tidak diubah)
├── web/                          monitor OPSIONAL
├── tools/  tests/  docs/  audio_storage/  captured_images/
```

## B. File dipindahkan

| Lama | Baru |
|---|---|
| `ai/deteksiObjek.py` | `ai/object_detection.py` |
| `ai/deteksiWajah.py` | `ai/expression_detection.py` |
| `ai/bacaTex.py` | `ai/sign_detection.py` (isinya deteksi rambu YOLO) |
| `ai/scene.py` | `ai/scene_segmentation.py` |
| `ai/provider.py` | `ai/llm.py` |
| `app_utils/camera.py` | `camera/webcam.py` |
| `app_utils/audio.py` | `services/audio.py` |
| `app_utils/esp32_bridge.py` | `devices/serial_bridge.py` |
| `speech/service.py` | `services/speech.py` |
| `session_manager.py` | `services/sessions.py` |
| `services/esp32_events.py` | `devices/events.py` |
| `server.py` + `server_multidevice.py` | `server/app.py`, `server/upload.py`, `devices/ws.py` |
| `esp32_quickbutton/esp32_quickbutton.ino` | `firmware/esp32_main/esp32_main.ino` |
| `esp32_multidevice/` | `firmware/esp32_multidevice/` |
| `esp32_tts_client.py`, `requirements-esp32-pcm.txt` | `tools/esp32_pcm_push.py`, `tools/requirements-esp32-pcm.txt` (alat manual, bukan runtime) |
| `README.md` | `docs/AI_DETAIL.md` (+ README baru) |
| `docs/{ESP32_QUICK_BUTTON,SOUND,VALIDASI,PERUBAHAN_AI}.md`, `AUDIT_SOURCE.json`, dll. | `docs/archive/` |
| `env.example` (+ `.env.example` lama digabung) | `.env.example` |

Pembaruan: deteksi teks/OCR dihapus. Key service `ocr` kini `sign`, class `TextReader` kini `SignDetector`, mode `baca`
kini `rambu`, event `text_detection` kini `sign_detection`, dan field `ocr_*`/`detected_text` kini `sign_*`.
Nama lama `text_detection`/`baca` hanya tersisa sebagai alias penerimaan (menjalankan deteksi rambu).

## C. Fungsi `web/app.js` — catatan historis dan status kini

Tabel berikut mendokumentasikan keadaan setelah migrasi backend. Dashboard
presentasi yang lebih baru mempertahankan komunikasi dan perintah perangkat,
tetapi menghapus akses kamera browser dan render snapshot.

| Fungsi | Peran | Status di sistem baru |
|---|---|---|
| `runQuick(mode)` | tombol layar → `/api/quick` | dipertahankan; request tanpa gambar memakai kamera backend |
| `handleEsp32Event` | SSE event fisik | hanya menampilkan; tidak memicu deteksi kedua |
| `initCamera`, `captureSnapshotBlob` | webcam browser | dihapus dari frontend |
| `startRecording`, `sendToServer` | pertanyaan suara opsional | dipertahankan untuk audio saja; tidak melampirkan snapshot |
| `connectWebSocket`, pairing | browser ↔ ESP32 | dipertahankan; koneksi dipantau untuk status dashboard |

## D. Fungsi dipindahkan / di-refactor

```
FILE LAMA → FUNCTION → PERMASALAHAN → FILE BARU → PERUBAHAN
```

| Lama | Function | Masalah | Baru | Perubahan |
|---|---|---|---|---|
| `web/app.js` | `runQuick` / `captureSnapshotBlob` (pemicu + kamera) | butuh browser + webcam | `services/quick_commands.py` `QuickCommandService.run` | kamera = `camera.capture_frames()` (ESP32-CAM); deteksi tak berubah (`ai/pipeline.py`) |
| `web/app.js` | `handleEsp32Quick` | browser jadi relay tombol | `devices/events.py` `ESP32EventHub.dispatch` → `QuickCommandService.start_from_button` | tombol fisik menjalankan deteksi langsung; balasan `quick_ack.delivered` = 1 jika dimulai |
| `services/quick_commands.py` | `register_quick_commands` (closure) | logika terkunci di route HTTP | kelas `QuickCommandService` + `register_quick_commands` | route memanggil `service.run`; snapshot kiriman klien tetap didukung, tanpa snapshot → ESP32-CAM |
| `app_utils/camera.py` | `Camera` (USB) | webcam lokal | `camera/esp32cam.py` `Esp32Cam` | antarmuka sama: `capture_frames`, `capture`, `get_frame`, `mjpeg_stream`, `get_status` |
| `server_multidevice.py` | `ws_endpoint` | `obstacle_warning` dianggap event tak dikenal → balasan `error` → beep salah, jarak tak tercatat | `devices/ws.py` | dicatat sebagai sampel jarak, tanpa balasan; ringkasan objek kini memakai sensor |
| `server_multidevice.py` | `/api/upload` | kamera dari browser | `server/upload.py` | isi tak diubah selain: `device_id` boleh ganti `session_id`; tanpa snapshot → `camera.capture()` |
| `session_manager.py` | — | ESP32 butuh pairing browser | `services/sessions.py` `ensure_device_session` | sesi otomatis `dev-<device_id>` |
| `server.py` | `/video_feed`, `/camera_status`, `/mp3` | webcam lokal | `server/app.py`, `server/upload.py` | `/video_feed` = pratinjau MJPEG dari ESP32-CAM |
| `server.py` / `server_multidevice.py` | `_is_text_question`, helper prompt, dsb. | duplikat dua server | `server/upload.py` | satu salinan |

**Pemicu pengganti untuk setiap tombol HTML:**

```
Tombol Objek   → Tombol fisik 1 → event object_detection     → mode "objek"
Tombol Ekspresi→ Tombol fisik 2 → event expression_detection → mode "ekspresi"
Tombol Rambu   → Tombol fisik 3 → event sign_detection       → mode "rambu"
```
Tombol HTML tetap ada di monitor opsional dan memakai jalur yang sama.

## E. File / endpoint dihapus (semuanya diverifikasi tidak diimpor/dipanggil)

| Item | Alasan |
|---|---|
| `ai/tanys.py` | shim kompatibilitas; tidak ada yang mengimpor |
| `app_utils/speaker_handler.py` | tidak diimpor; `docs/PERUBAHAN_AI.md` sendiri menyebutnya akan dihapus |
| `databse.py` | shim salah-eja; pemanggil (`server*.py`) diganti langsung ke `services.database` |
| `server.py`, `server_multidevice.py` | digabung ke `server/`, `devices/ws.py` |
| `tools/benchmark_ocr.py`, `--ocr` di `prepare_models.py`, `easyocr` di `requirements.txt`, variabel `OCR_*` (kecuali `OCR_SPOKEN_MAX_WORDS`) | EasyOCR sudah tidak dipakai; `ai/sign_detection.py` memakai YOLO |
| 15 test OCR/EasyOCR di `test_ai_improvements.py` & `test_vision.py` | menguji metode yang sudah tidak ada (`_run_easyocr`, `_merge_results`, `_reading_order`); gagal sebelum migrasi |
| Endpoint server tunggal `/set_mode`, `/process_current_mode`, `/upload` (audio mentah), `/api/legacy_status`; `/api/detect_frame` | bergantung webcam server / duplikat; fungsinya dicakup `/api/quick` dan `/api/upload`. **Beri tahu bila ada klien yang memanggilnya.** |
| `ESP32_BROADCAST_FALLBACK`, `SERVER_MODE` | tidak relevan lagi |
| **Tidak disalin ke hasil:** `esp32_quickbutton/secrets.h` (berisi kredensial Wi-Fi/token asli), `audio_storage/*` (rekaman suara & MP3) | keamanan/privasi; salin `secrets.h` Anda sendiri dari arsip lama |

`web/` **tidak dihapus** (dijadikan opsional, mati secara default). Aman dihapus jika tak dipakai: tidak ada kode
runtime yang mengimpornya (`WEB_UI_ENABLED=false` → tidak disajikan).

## F. Alur baru

```
Tombol fisik 1/2/3
   → ESP32 utama (debounce, beep)
   → WebSocket {"type":"quick_command", ...}
   → devices/ws.py → ESP32EventHub.dispatch (normalisasi, cooldown, sesi dev-<id>)
   → QuickCommandService.start_from_button (thread pekerja; 1 pekerjaan visi aktif)
   → Esp32Cam.capture_frames(n) → GET /capture ×n
   → VisionPipeline.run (YOLOv5n / ekspresi / rambu / SegFormer + sensor jarak)
   → TTS MP3 (audio_storage/quick_*.mp3)
   → {"type":"tts_ready","url":"/api/quick_audio_file/..."} → ESP32 mengunduh & memutar
```

## G. Dependency

| | Status | Catatan |
|---|---|---|
| Satu mesin menjalankan `main.py` (laptop/mini-PC/RPi/VPS) | **wajib** | AI tidak muat di ESP32/ESP32-CAM |
| Web server | **wajib sebagai API Flask** | hanya WebSocket/HTTP untuk ESP32; **tanpa** halaman web |
| Folder `web/`, browser | tidak | monitor opsional |
| Webcam | tidak | `CAMERA_SOURCE=webcam` hanya untuk uji |
| Jaringan Wi-Fi/LAN yang sama (ESP32, ESP32-CAM, backend) | **wajib** | pull HTTP; VPS butuh VPN/tunnel ke kamera |
| Internet | **wajib untuk suara** (Edge TTS/gTTS) | deteksi tetap jalan tanpa internet, suara tidak |
| Groq | opsional | hanya tanya-jawab/STT; tiga tombol tidak memakainya |
| MongoDB, Ubidots | opsional | kegagalan tidak memengaruhi hasil |
| Serial USB | opsional | tanpa suara ke ESP32 |
| Kalibrasi jarak ESP32-CAM | disarankan | tanpa itu tidak ada angka cm dari kamera |

## Perubahan perilaku yang perlu Anda ketahui

- Tombol fisik kini mendapat umpan balik jujur: `delivered:1` hanya bila perintah benar-benar dimulai.
- Kegagalan kamera/AI diumumkan lewat suara (atau beep rendah bila TTS mati), bukan diam.
- Tombol saat proses lain berjalan ditolak (bukan diantre); cooldown 700 ms tetap.
- Bug kecil diperbaiki: debounce hub memakai nilai awal 0 sehingga tekanan pertama bisa diabaikan bila jam monotonic < cooldown (mis. tepat setelah Windows menyala).
- Ringkasan objek kini benar-benar memakai sensor ultrasonik dari firmware `esp32_main` (sebelumnya sampel tak tercatat).

## Verifikasi

- Python: 99 test lulus (dengan stub `torch` di sandbox; 77 tanpa torch). Satu test model asli
  (`test_bundled_emotion_state_dict_and_blank_frame`) **tidak dapat dijalankan** tanpa torch sungguhan.
- Uji ujung-ke-ujung (`tests/test_e2e_network.py`): klien WebSocket → backend → HTTP ke ESP32-CAM palsu → MP3 diunduh; kamera mati diumumkan; sampel jarak.
- Uji mutasi: menonaktifkan pengumuman error / penanganan `obstacle_warning` membuat test gagal (test tidak kosong).
- Node: `test_frontend.cjs`, `test_esp32_frontend.cjs` lulus.
- `python main.py` dijalankan sungguhan: `/api/health` normal, tombol via HTTP memulai pekerjaan, kamera mati ditangani.
- Firmware `esp32_cam` **dikompilasi** (core 2.0.17 dan 3.0.7, board esp32cam). `esp32_main` tidak diubah.
- **Belum diuji:** perangkat keras nyata (ESP32-CAM, tombol, speaker), model AI asli (YOLO/CNN/rambu/SegFormer)
  pada pipeline baru, latensi Wi-Fi sebenarnya.
