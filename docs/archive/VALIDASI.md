> **Arsip.** Dokumen sejarah sebelum migrasi ke ESP32-CAM; path dan alur (browser/webcam) merujuk tata letak lama. Lihat `docs/MIGRASI.md` untuk pemetaan file lama → baru.

# Audit dan validasi perubahan

## Lingkup yang dipelajari

Kode aplikasi ditelaah untuk entry point, dua server, kepemilikan kamera,
session/pairing, dispatch mode, inferensi, speech, database, dan frontend.
Seluruh file Python, termasuk vendor YOLOv5 untuk train/export/logging, dibaca
dan diparse oleh audit AST. Source vendor runtime juga diperiksa pada jalur
AutoShape, preprocessing, loader checkpoint dan requirements. Ini **bukan**
klaim telah menjalankan training/export atau menguji semua cabang vendor.

85 file Python pada paket hasil lolos parsing, termasuk tools dan tests baru.
Script `tools/audit_project.py` mencetak inventaris, jumlah fungsi/kelas dan
SHA256 agar audit dapat diulangi.

## Temuan awal dan tindakan

| Temuan | Tindakan |
|---|---|
| Model COCO tidak mengenali tembok/pintu | SegFormer B0 ADE20K tambahan, dengan peringatan bila tidak aktif |
| Fallback YOLO menurunkan threshold ke 0.14 | Recovery menaikkan detail/resolusi, tidak menurunkan ambang utama |
| Stabilizer mengembalikan kandidat lemah saat hasil stabil kosong | Bypass dihapus; Quick Command memakai konfirmasi burst tanpa cache lintas sesi |
| `torch.load` diubah global ketika YOLO dimuat | Compatibility `weights_only=False` hanya di loader YOLO lokal tepercaya |
| Vendor bergantung `pkg_resources` lama | Parse versi memakai `packaging.version`, tanpa menghapus dependency setuptools |
| Legacy single mengklasifikasikan seluruh frame sebagai emosi | Cari crop wajah utama dahulu, kemudian CNN dan confidence/margin gate |
| UI wajah multi umumnya menghitung wajah | Mode ekspresi terpisah; hitung wajah lama tetap tersedia tanpa CNN |
| Model dimuat sekaligus saat startup | Lazy import/load dan retry terisolasi per model |
| Teks OCR semua digabung menjadi satu baris | Rekonstruksi baris, koreksi duplikat kotak yang berselisih bacaan |
| Antrean inference/TTS bisa menahan respons | Busy guard vision, output background dengan antrean terbatas |
| Jarak sensor belum menjadi data sesi segar | Sampel per pasangan, TTL, validasi nilai dan pengecekan ulang seusai inference |
| Sensor bisa salah tertaut setelah pairing ulang | Bersihkan pasangan lama dan sampel jaraknya |

Tidak dilakukan training ulang, confidence calibration dengan validation set,
atau pengukuran precision/recall lapangan. Nama kelas/order CNN mengikuti kode
asli: marah, jijik, takut, senang, sedih, kaget, netral; asal dataset tidak ada.

## Hasil pengujian

- **42 tes Python lulus**: konsensus, tidak menghidupkan kembali kotak lama,
  threshold jarak terkalibrasi 50 cm secara matematis, clipping/resolusi/ID
  kamera, pembatasan wajah tengah/samping/kecil, crop-only CNN, penghitungan
  legacy, urutan OCR, overlap semantik berbasis mask, dispatch satu mode,
  input rusak, model gagal, request duplikat, backpressure dua sesi,
  pairing ulang, TTL sensor sebelum/sesudah inference, output TTS/DB async
  dengan mock dan routing ke ESP32.
- Tes Node interaksi frontend lulus: tiga mode, tiga snapshot objek/satu
  snapshot ekspresi dan OCR, satu pemilik kamera, klik ganda, recovery error,
  penghentian track. Ini menggunakan DOM/media mock, bukan browser fisik.
- `node --check web/app.js` lulus.
- Import dua Flask server dan health endpoint lulus tanpa webcam/API key.
  Tes startup juga berhasil ketika dependency speech/database belum tersedia:
  layanan terkait nonaktif, UI/health masih dapat diakses.
- **Inferensi YOLO nyata** berhasil pada `bus.jpg` dan `zidane.jpg` bawaan.
  Hasil masing-masing 5 dan 2 kotak pada konfigurasi default pengujian.
- **CNN nyata**: state_dict cocok semua layer; softmax bekerja. Frame kosong
  tidak menghasilkan wajah. Tidak ada dataset wajah berlabel untuk menilai
  akurasi lima/tujuh ekspresi.
- **SegFormer nyata**: model safetensors berhasil dimuat dan menjalankan
  inference. Pada foto bus, tidak ada area prioritas tambahan yang lolos.
  Filtering area wall diuji dengan mask sintetis. Belum ada benchmark foto
  tembok/pintu dari kamera pengguna untuk menghitung recall/false positive.
- **EasyOCR nyata** membaca gambar teks sintetis dua baris tepat sebagai
  `JALAN KELUAR` dan `EXIT 50 CM`, dengan newline dipertahankan.
- Smoke test tiga mode diulang dua kali dan lulus setelah dependency lengkap.

Lingkungan uji: Linux Python 3.12.13, torch 2.13.0+cpu, torchvision 0.28.0+cpu,
OpenCV headless 4.14.0, NumPy 2.3.5, EasyOCR 1.7.2,
Ultralytics 8.4.135, Transformers 4.57.6. Target default instalasi tetap
Python 3.11/3.12; kesesuaian OS/CUDA harus diperiksa pada perangkat pengguna.

## Waktu yang teramati (contoh, bukan jaminan)

Pada smoke test satu gambar dan CPU 2 thread, pemanggilan kedua:

| Mode | Waktu proses teramati |
|---|---:|
| Objek + segmentasi | 814 ms |
| Ekspresi pada frame tanpa wajah | 6 ms |
| OCR teks dua baris | 12.642 ms (12,6 detik) |

Pemanggilan pertama masing-masing sekitar 12,1 detik, 87 ms, dan 19,7 detik
karena import/load model. Angka **bukan** perbandingan dengan versi lama,
bukan latency webcam ujung-ke-ujung, tidak mencakup upload/TTS, dan bukan
pengukuran burst tiga frame. Waktu dapat berubah menurut hardware/input.
Profil OCR menunjukkan CRAFT/konvolusi CPU sebagai biaya utama. UI tetap
responsif, tetapi belum ada klaim OCR realtime; CUDA dapat membantu bila ada.

## Yang belum dapat diverifikasi

1. Jangkauan/akurasi pada 50 cm, 75 cm, 1 m dengan webcam fisik pengguna.
2. Presisi tembok/pintu, penghalang, dan ekspresi di kondisi nyata.
3. Izin kamera browser, perangkat USB putus-pasang, serta pratinjau di Windows.
4. Groq/STT, TTS online, MongoDB/Ubidots memakai kredensial nyata.
5. ESP32, ultrasonik, speaker MAX98357A dan emergency audio secara hardware.
6. Layout browser visual, touch/mobile, dan kamera nyata; tes frontend hanya mock.

Gunakan protokol uji pada README sebelum pemakaian lapangan. Sistem tidak
dapat menyatakan “jalan aman”, meskipun tidak menemukan objek.

## Integrasi/data lama

`services/database.py` dipertahankan, termasuk pengiriman background dashboard
lama. Kode asli mengirim record netral contoh setiap menit; record tersebut
**bukan** hasil ekspresi wajah nyata. Quick Command mencatat `faces` hasil
pengamatan pada `Interaksi`. Jangan memakai telemetry contoh tersebut untuk
menilai akurasi ekspresi tanpa memisahkannya dari data observasi.

Model hasil unduhan pengujian tidak dikemas ulang. Siapkan lewat
`tools/prepare_models.py` untuk memperoleh file dari sumber aslinya dan
memperhatikan lisensi. Tidak ada kredensial/foto webcam baru dalam paket.
