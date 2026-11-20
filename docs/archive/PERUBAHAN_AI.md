> **Arsip.** Dokumen sejarah sebelum migrasi ke ESP32-CAM; path dan alur (browser/webcam) merujuk tata letak lama. Lihat `docs/MIGRASI.md` untuk pemetaan file lama → baru.

# Perubahan AI dan hasil pemeriksaan

## Ruang lingkup dan hasil audit

Arsip awal dipetakan sebelum source diubah: 90 file Python diparse dengan AST,
termasuk source YOLOv5 yang dibundel. Import, pemanggilan fungsi, loader dinamis,
path model, endpoint, frontend, konfigurasi, dan firmware diperiksa.
`AUDIT_SOURCE.json` berisi inventaris import/call per source pada hasil akhir.

Arsitektur utama tetap sebagai berikut:

| Komponen | Hubungan dan penggunaan |
|---|---|
| `main.py` | Memilih `server_multidevice.py` (default) atau `server.py` melalui `SERVER_MODE`; memulai tugas database latar belakang. |
| `config/settings.py`, `env.example` | Memuat `.env`, kunci Groq, MongoDB, Ubidots, speaker, server, ESP32. |
| `ai/runtime.py` | `LazyService` mengimpor kelas detektor berdasarkan nama modul; kegagalan satu model tidak menjatuhkan server. |
| `services/quick_commands.py` | Endpoint `/api/quick` bersama; menjaga request idempotent, mencegah antrean vision menumpuk, menyimpan hasil per sesi, menjalankan TTS/pencatatan di latar belakang. |
| `ai/pipeline.py` | Memilih satu mode: objek, ekspresi, atau baca; menggunakan `ai/spatial.py` untuk posisi/jarak objek. |
| `web/app.js`, `web/index.html` | Browser memiliki webcam pada mode multi, mengambil snapshot, memanggil `runQuick`, menampilkan hasil dan memutar audio. |
| `app_utils/camera.py` | Satu pemilik webcam server pada mode single; capture, burst, reconnect, dan MJPEG. |
| `services/esp32_events.py`, `app_utils/esp32_bridge.py` | HTTP/WebSocket/serial → normalisasi event → SSE → `runQuick`; mapping 1 objek, 2 ekspresi, 3 tulisan. |
| `session_manager.py` | Sesi, pairing ESP32, sensor, request dedupe, dan konteks visual terakhir. |
| `ai/provider.py`, `ai/tanys.py` | Groq untuk pertanyaan/konteks visual; `tanys.py` tetap sebagai compatibility shim. |
| `app_utils/audio.py`, `speech/service.py` | Groq Whisper STT, Edge TTS → gTTS, MP3, speaker lokal/browser/ESP32. |
| `databse.py`, `services/database.py` | Import dinamis memakai ejaan `databse`; shim wajib dipertahankan. MongoDB Interaksi dan tugas latar belakang, pengiriman Ubidots tetap ada. |
| `esp32_quickbutton/`, `esp32_multidevice/` | Dua firmware alternatif dengan event QuickButton, speaker dan komunikasi server. Keduanya masih relevan. |
| `esp32_tts_client.py` | Klien PCM opsional untuk endpoint `/tts/play` yang masih ada pada firmware QuickButton. Bukan import runtime utama, tetapi bukan file mati. |
| `model/yolov5/`, bobot `.pt`/`.pth`, Haar XML | Dependency lokal dan model; loader hub, pickle/checkpoint serta YAML memerlukan lebih dari import eksplisit. Seluruh source vendor dipertahankan. |
| `tests/`, `tools/`, `docs/` | Test regresi, diagnostik hardware/DB/model, kalibrasi, penyiapan model dan dokumentasi masih digunakan secara mandiri. |

## File yang dimodifikasi

| File | Perubahan dan alasan |
|---|---|
| `ai/deteksiWajah.py` | Keputusan tiga ekspresi, pemeriksaan kualitas crop, tiga crop kecil dalam satu batch, konfirmasi burst tanpa sejarah lintas sesi, keluaran pendek. |
| `ai/bacaTex.py` | Gambar asli dahulu, peningkatan ringan sebagai fallback, parameter EasyOCR, pembatasan pass/waktu, tile opsional, dedupe yang mempertahankan teks lengkap, penolakan skor nonfinite. |
| `ai/pipeline.py` | Jumlah frame per mode; burst ekspresi; bacaan ringkas dan penerusan warning OCR. |
| `ai/provider.py` | Instruksi suara pendek, tanpa skor model; konteks OCR ke Groq tetap dipertahankan; fallback ringkas dan label objek Bahasa Indonesia yang sudah tersedia. |
| `ai/spatial.py` | Hilangkan pembuka dan confidence; maksimal tiga jenis/posisi unik; informasi halangan/jarak yang relevan tetap ada. |
| `services/quick_commands.py` | Menerima 1–3 snapshot ekspresi; konfigurasi frontend mengumumkan jumlah frame; batas snapshot 1920 piksel untuk detail teks. |
| `server.py` | Jalur legacy ekspresi webcam memakai burst; bacaan OCR ringkas; routing kata “ekspresi”. Signature lama tetap dapat dipakai. |
| `server_multidevice.py` | Kata “ekspresi” diarahkan ke detektor ekspresi; fallback OCR ringkas. Endpoint upload satu-frame lama tetap didukung. |
| `web/app.js` | Ambil burst ekspresi, hapus angka confidence dari overlay; listener dan jalur ESP32 tetap sama. |
| `web/index.html` | Hapus atribut `hidden` dari tiga tombol Quick Command supaya dapat dipakai mouse/touch pada panel Pengaturan & kontrol. |
| `env.example` | Dokumentasi parameter baru; seluruh kunci integrasi lama tetap ada. |
| `README.md` | Informasi perilaku baru dan tautan ke laporan ini; koreksi petunjuk ekspresi/OCR yang tidak lagi sesuai. |
| `tests/test_frontend.cjs` | Verifikasi payload ekspresi tiga frame. |
| `tests/test_esp32_frontend.cjs` | Verifikasi burst ekspresi, tunggu kondisi UI alih-alih waktu tetap, tutup koneksi simulasi setelah test. |

File baru: `ai/output.py` (pemformatan suara), `tests/test_ai_improvements.py`
(test perilaku baru), `requirements-esp32-pcm.txt` (dependency jalur PCM opsional),
`tools/benchmark_ocr.py` (fixture OCR yang dapat diulang), dan laporan/inventaris di `docs/`. Daftar hash sebelum/sesudah ada dalam
`MANIFEST_PERUBAHAN.json`.

## File yang dihapus

- `app_utils/speaker_handler.py`: tidak ada import, pemanggilan `SpeakerHandler`,
  referensi nama file, atau pemakaian path outputnya dari source, frontend,
  firmware maupun dokumentasi lain. Implementasi ini menduplikasi gTTS/pygame
  secara blocking dengan nama file sementara tetap. Jalur aktif adalah
  `app_utils/audio.py` → `speech/service.py`.
- 35 file bytecode `.pyc` di folder `__pycache__`: cache hasil kompilasi Python
  3.11, bukan source/dependency. Sebagian timestamp cache bahkan lebih baru
  daripada source; cache lama tidak dipakai sebagai sumber implementasi.

Daftar persis file yang dihapus disertakan dalam manifest. Rekaman audio pengguna
pada arsip awal dipertahankan: bisa masih dirujuk riwayat MongoDB yang tidak
tersedia untuk diperiksa. Kedua firmware, shim kompatibilitas, alat diagnostik,
model dan contoh gambar YOLO juga dipertahankan. Tidak ada dependency utama
atau konfigurasi environment pengguna yang dihapus.

## Peningkatan ekspresi

Bobot lama berisi delapan tensor state dict dengan output **tujuh logit**, cocok
dengan `SmallEmotionCNN`. Tidak ada metadata label, data pelatihan atau dataset
validasi di arsip. Urutan label mengikuti implementasi awal; tidak ada pelatihan
ulang atau penggantian model yang tidak dapat diverifikasi.

Output publik dibatasi pada **netral, senang, sedih**, atau “belum terlihat jelas”.
Empat logit lain tetap dihitung karena diperlukan oleh checkpoint. Skornya tidak
dibuang lalu dinormalisasi ulang, sehingga kelas target tidak mendapat keyakinan
palsu. Ekspresi lain juga tidak dipaksa menjadi netral.

- Netral: ambang awal 0,55 dengan selisih dua skor teratas 0,15.
- Senang/sedih: ambang lebih ketat 0,70 dan selisih 0,20, untuk mengurangi salah
  klasifikasi terhadap netral. Nilai ini heuristik awal, belum hasil kalibrasi.
- Haar frontal dan filter posisi/ukuran tetap memilih wajah utama di tengah.
  Wajah terlalu jauh, di tepi atau terpotong ditolak seperti sebelumnya.
- Crop terlalu gelap/terang, kontras rendah atau sangat kabur tidak mendapat
  ekspresi pasti. Ini menghindari jawaban percaya diri dari input buruk.
- Satu inferensi batch terhadap tiga variasi crop kecil mengurangi sensitivitas
  terhadap pergeseran kotak. Pada cahaya kurang ideal, hanya satu variasi memakai
  CLAHE ringan; hasil gambar asli harus setuju dengan hasil gabungan.
- QuickButton mengambil maksimal tiga frame dengan jeda sekitar 100 ms.
  Perlu dua pengamatan andal yang sepakat dan frame terbaru harus setuju.
  Kotak mengikuti track berdekatan; wajah yang hilang memutus track. Tidak ada
  smoothing global yang mencampur sesi, kamera atau klik sebelumnya.
- Endpoint lama yang hanya menerima satu gambar tetap memakai pemeriksaan
  kualitas/crop, tetapi **tidak** mengklaim konfirmasi temporal. Jalur ekspresi
  webcam single dan QuickButton sudah memakai burst.

Pencocokan kotak bukan pengenalan identitas atau pengukuran sudut kepala.
Kestabilan lebih konservatif dapat meningkatkan frekuensi jawaban belum jelas.
Ambang perlu diuji dengan pengguna/kamera sesungguhnya sebelum diturunkan.

## Peningkatan OCR

EasyOCR `en+id` tetap digunakan dengan detektor CRAFT. Pemindaian pertama memakai
grayscale asli yang diresize secara terbatas, tanpa threshold/bilateral/sharpen
agresif. Canvas dapat mempertahankan detail hingga 1920 piksel, alokasi input
sekitar 2,4 megapiksel, dan `min_size=8`. `contrast_ths=0.15` mengaktifkan bantuan
kontras EasyOCR pada kotak yang membutuhkannya.

Pembacaan tambahan dipertimbangkan jika hasil kosong, sedikit, kecil, tidak
meyakinkan, gambar sempat diperkecil atau pencahayaan kurang ideal. Fallback memakai
CLAHE ringan dan unsharp ringan; pembesaran 1,5x hanya dilakukan di pass tambahan.
Versi awal percobaan membesarkan seluruh input, tetapi ditolak karena latensinya
hampir dua kali lipat pada fixture teks jelas.

Default maksimal dua pass. Anggaran lima detik membatasi **dimulainya** pass
berikutnya, termasuk estimasi biaya pass tambahan dari durasi pass pertama;
bukan timeout keras atas inferensi yang sudah berjalan. Jika pass
pertama sudah lambat, tidak ada pass tambahan. Mode detail opsional memakai empat
tile tumpang tindih dari keempat sudut dengan koordinat dikembalikan ke frame asli.
Tile tidak berjalan default. Jika waktu/pass habis sebelum selesai, UI menerima
warning; sistem tidak mengaku sudah memindai semua area tambahan.

CRAFT tetap menangani kotak teks agak miring. `OCR_ROTATIONS=90,180,270` tersedia
untuk orientasi tegak/terbalik, tetapi lebih lambat. Ini bukan koreksi perspektif
berat. Beberapa baris disusun berdasarkan geometri; tata letak tabel/kolom kompleks
masih perlu uji khusus.

Hasil beberapa pass digabung berdasarkan lokasi dan kesamaan tulisan, bukan
berdasarkan tulisan saja: kata yang sama pada dua lokasi tetap dipertahankan.
Pembacaan parsial di dalam kotak teks lengkap tidak otomatis mengganti teks
lengkap hanya karena skornya sedikit lebih tinggi. Bila fallback gagal, teks awal
masih tersedia. Skor NaN ditolak.

`detected_text`, kotak, baris dan metadata confidence tetap ada untuk aplikasi,
riwayat dan konteks AI. Pertanyaan OCR pada jalur suara/upload tetap meneruskan
teks ke `GroqAI.ask_with_context`; hasil QuickButton tersimpan di sesi untuk
pertanyaan lanjutan. QuickButton tetap membaca hasil OCR lokal secara langsung,
sesuai arsitektur awal, sehingga tidak menunggu panggilan Groq tambahan.

Pembesaran tidak dapat memulihkan detail huruf yang tidak terekam sensor. Tidak
ada jaminan semua tulisan, jarak tertentu, atau kondisi cahaya ekstrem bisa dibaca.

## Output AI/speaker

- Contoh ekspresi: `Wajah terlihat senang.`
- Contoh objek: `Ada kursi di depan.` Posisi/halangan dan estimasi jarak yang
  memang tersedia tetap dipertahankan, tanpa menyamakan sensor dengan objek YOLO.
- OCR langsung dibaca, tanpa “Saya berhasil membaca...” atau confidence.
- Secara default, bacaan OCR langsung maksimal 100 kata dengan penanda
  `Masih ada lanjutan teks.` bila dipotong. Ini **cuplikan**, bukan ringkasan AI;
  teks lengkap tetap ada di `detected_text`. Ubah `OCR_SPOKEN_MAX_WORDS` bila perlu.
- Groq diarahkan menjawab 1–3 kalimat pendek; keluaran dibersihkan dari komentar
  confidence, pembuka tertentu, format dekoratif, dan kalimat duplikat.
- Persentase yang benar-benar tercetak pada teks, misalnya `DISKON 50%`, tidak
  dihapus dari bacaan OCR. Angka jarak/harga/jam juga bukan confidence.
- Confidence tetap disimpan sebagai metadata internal, tetapi tidak ditampilkan
  pada overlay pengguna atau dibacakan dalam template hasil.

## Dependency dan menjalankan

Tidak ada model/library AI baru. Pakai environment Python 3.11 yang direkomendasikan
proyek; `.env` lama tetap dipakai. Tambahkan parameter baru seperlunya, jangan
menimpa kunci API yang sudah Anda isi.

```bash
pip install -r requirements.txt
python tools/prepare_models.py --ocr
python main.py
```

Bobot CNN/Haar/YOLO asli tetap di ZIP. Bobot EasyOCR yang diunduh untuk pengujian
serta SegFormer tidak digandakan ke ZIP. Penyiapan OCR sekarang eksplisit agar
penekanan pertama tombol tidak memulai unduhan besar. Jika ingin perilaku unduh
otomatis lama, set `OCR_DOWNLOAD_ENABLED=true`.

Untuk segmentasi tembok/pintu yang memang sudah menjadi fitur opsional proyek:

```bash
python tools/prepare_models.py --scene
```

Untuk jalur **opsional** push PCM `esp32_tts_client.py`:

```bash
pip install -r requirements-esp32-pcm.txt
```

Jalur PCM juga membutuhkan `ffmpeg` di PATH sistem (Linux: `sudo apt install ffmpeg`).
QuickButton normal memakai MP3 dan tidak memerlukan pydub/ffmpeg.

Pengujian ulang:

```bash
pip install pytest
python -m pytest
node tests/test_frontend.cjs
node tests/test_esp32_frontend.cjs
python tools/audit_project.py
python tools/check_object_detection.py
python tools/check_quick_commands.py --repeat 1
```

Perintah terakhir memerlukan bobot OCR dan SegFormer bila segmentasi diaktifkan.
Untuk uji tiga mode tanpa segmentasi, set `SCENE_SEGMENTATION=false` di environment.

## Hasil pengujian dan batas verifikasi

- **69 test Python lulus**, tanpa skip: routing satu mode, burst/ketidakpastian
  ekspresi, kualitas wajah, state dict asli, geometri/dedupe/anggaran OCR,
  format suara, payload OCR ke Groq, pairing event, kontrak MongoDB/Ubidots,
  idempotensi, backpressure, sensor, audio background dan pemulihan error.
- **Dua test Node.js lulus**: pergantian tiga mode tanpa membuka ulang kamera,
  burst, tombol sibuk, putus jaringan, SSE ESP32, mapping tombol, dan tombol
  mouse tetap berfungsi setelah jalur ESP32 terputus.
- **92 file Python akhir berhasil diparse**. Seluruh fungsi lama pada modul
  yang diubah dipertahankan. Tidak ada referensi runtime ke source yang dihapus.
- **YOLO asli berhasil inferensi** pada `bus.jpg` dan `zidane.jpg`.
- **Smoke pipeline tiga mode lulus** dengan segmentasi dinonaktifkan dan
  sekali lagi dengan SegFormer diaktifkan setelah bobot disiapkan: deteksi
  objek, tidak mengada-ada wajah pada frame kosong, dan OCR tepat untuk
  `JALAN KELUAR` / `EXIT 50 CM`.
- **Kedua server berhasil diimpor**, halaman utama, health dan client config
  memberi HTTP 200. Route legacy, `/api/quick`, SSE dan route audio tetap ada.
- **Groq/MongoDB/Ubidots diuji dengan mock**, bukan koneksi akun nyata.
  Payload dan pemanggilan SDK/API diperiksa; tidak ada kredensial akun aktif
  di arsip. TTS online, speaker fisik, webcam USB dan firmware ESP32 tidak
  diverifikasi langsung karena perangkat/akun tersebut tidak tersedia.
- Source `main.py`, konfigurasi utama, kamera, event hub/serial, audio/STT/TTS,
  database/Ubidots, session manager, model objek dan segmentasi diperiksa
  identik byte-per-byte dengan versi awal.

`docs/HASIL_UJI.txt` dan `docs/OCR_COMPARISON.json` menyertakan hasil uji.
Runtime aktual: Python 3.12.14, NumPy 2.2.6, OpenCV 4.11.0,
PyTorch 2.6.0 CPU, torchvision 0.21.0, EasyOCR 1.7.2, Groq SDK 1.7.0,
PyMongo 4.18.0. Python 3.11 masih target instalasi proyek, tetapi tidak diuji
secara terpisah pada pekerjaan ini.

### Perbandingan OCR nyata pada fixture sintetis

Reader/bobot EasyOCR dan frame 1280×720 sama untuk kedua implementasi.
CER adalah jarak edit karakter dibagi panjang teks referensi setelah huruf
besar dan whitespace dinormalisasi. Tidak mengukur akurasi kamera lapangan.
Angka waktu adalah satu putaran di lingkungan CPU bersama, bukan SLA atau
bukti bahwa setiap perangkat akan lebih cepat.

| Contoh | CER lama | CER baru | Waktu lama | Waktu baru |
|---|---:|---:|---:|---:|


| Dua baris jelas | 0.0000 | 0.0000 | 8.68 s | 6.96 s |
| Tiga baris tulisan kecil | 0.2500 | 0.0000 | 6.73 s | 8.75 s |
| Miring 12 derajat | 0.0000 | 0.0000 | 7.48 s | 4.40 s |
| Kontras rendah | 0.0000 | 0.0000 | 5.04 s | 7.83 s |
| Judul besar + tulisan kecil | 0.0345 | 0.0345 | 5.28 s | 6.13 s |
| Gambar kosong | 0.0000 | 0.0000 | 13.94 s | 6.35 s |

Pada fixture tulisan kecil, baris `Diskon 50%` yang sebelumnya hilang sekarang
terbaca. Pada campuran ukuran masih ada kesalahan `Rapat` → `Ropot`; CER tetap
sama, sehingga tidak dinyatakan membaik pada semua contoh. Semua enam fixture
pada putaran final memakai satu pass pada versi baru karena aturan kebutuhan
atau batas biaya; fallback dan tile diuji terpisah melalui test perilaku/mock.
Belum ada data wajah berlabel untuk mengukur akurasi netral/senang/sedih atau
false-positive netral secara empiris.

Ulangi fixture setelah menyiapkan bobot OCR:

```bash
python tools/benchmark_ocr.py --output ocr_benchmark.json
# Opsional: bandingkan dengan ekstraksi arsip versi asli.
python tools/benchmark_ocr.py --baseline-root /path/pytest-asli --output perbandingan.json
```

Untuk validasi lapangan, ambil beberapa burst masing-masing netral, senang,
sedih pada cahaya terang/redup dan posisi sedikit bergeser. Catat salah label,
jumlah belum jelas dan waktu respons. Untuk OCR, gunakan teks referensi yang
sama pada jarak berbeda; catat kata/baris yang terlewat. Jangan menyamakan
hasil test logika atau fixture sintetis dengan pengukuran akurasi tersebut.

## Referensi parameter

- [Dokumentasi resmi EasyOCR](https://www.jaided.ai/easyocr/documentation/):
  `canvas_size`, `mag_ratio`, `min_size`, kontras, rotasi dan keluaran kotak.
- [PyTorch: penyimpanan/pemuatan state dict](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html):
  pemuatan bobot ke arsitektur yang sama dan mode evaluasi.
