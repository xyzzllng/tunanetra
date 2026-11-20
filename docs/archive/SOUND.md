> **Arsip.** Dokumen sejarah sebelum migrasi ke ESP32-CAM; path dan alur (browser/webcam) merujuk tata letak lama. Lihat `docs/MIGRASI.md` untuk pemetaan file lama → baru.

# Configuration Guide & Troubleshooting Audio (ESP8266Audio on ESP32 WROOM-32)

Dokumen ini berisi panduan konfigurasi audio, tuning performa buffer, serta panduan *troubleshooting* jika mengalami masalah suara patah-patah (*stuttering*), *glitch*, atau audio terulang-ulang (*looping bug*) akibat kendala sinyal Wi-Fi / jaringan.

---

## 🛠️ Ringkasan Implementasi Audio

Proyek ini menggunakan library **ESP8266Audio** oleh *earlephilhower* yang bekerja pada ESP32 DevKit V1 (WROOM-32) tanpa membutuhkan PSRAM eksternal.

### Komponen Audio yang Digunakan:
* **`AudioFileSourceHTTPStream`**: Mengambil *stream* MP3 dari server HTTP.
* **`AudioFileSourceBuffer`**: Menampung data *stream* sementara di RAM (SRAM) untuk mencegah *underrun*.
* **`AudioGeneratorMP3`**: Melakukan dekoding format MP3.
* **`AudioOutputI2S`**: Mengirimkan data PCM audio ke DAC/Amplifier MAX98357A via protokol I2S.

---

## 🎛️ Konfigurasi Parameter Audio

Di dalam kode program (`.ino` atau file header terkait), terdapat beberapa konstanta yang dapat disesuaikan:

```cpp
// Ukuran buffer streaming audio (dalam Byte)
#define HTTP_BUFFER_SIZE 4096  

// Gain / Volume Audio (1.0 = Default, 1.5 = 150%, 2.0 = 200%)
#define AUDIO_GAIN 1.0        
```

### 1. `HTTP_BUFFER_SIZE` (Buffer Jaringan)
* **Default:** `4096` byte (4 KB)
* **Fungsi:** Menyimpan sementara paket data MP3 dari server sebelum didekoding.
* **Rekomendasi Tuning:**
  * Jika koneksi Wi-Fi stabil: `4096` sudah cukup.
  * Jika sinyal Wi-Fi lemah / latency tinggi: Naikkan ke `8192` (8 KB) atau `16384` (16 KB).
  * *Catatan:* ESP32 WROOM-32 memiliki SRAM tersisa ~320 KB, sehingga menaikkan buffer ke 8KB–16KB sangat aman dan tidak menyebabkan *Out Of Memory* (OOM).

### 2. `AUDIO_GAIN` (Pengatur Gain/Volume)
* **Default:** `1.0`
* **Panduan Pengaturan:**
  * `0.5` - `1.0`: Suara jernih, volume standar.
  * `1.2` - `1.5`: Mengencangkan suara jika speaker kecil kurang nyaring.
  * `> 2.0`: **Tidak direkomendasikan** karena berpotensi menyebabkan *clipping* (suara pecah/distorsi).

---

## 🐛 Troubleshooting & Masalah Umum Audio

### 🚨 1. Suara Patah-Patah / Stuttering / Gemericik (Wi-Fi Sinyal Lemah)

**Penyebab:**
Kecepatan transfer data Wi-Fi lebih lambat daripada kecepatan *playback* MP3 (*buffer underrun*). Ketika data di buffer habis sebelum paket Wi-Fi berikutnya tiba, dekoder MP3 akan kehilangan frame data sehingga suara menjadi terputus-putus atau berisik.

**Solusi:**
1. **Perbesar Ukuran Buffer:**
   Ubah `HTTP_BUFFER_SIZE` dari `4096` menjadi `8192` atau `16384`:
   ```cpp
   #define HTTP_BUFFER_SIZE 8192
   ```
2. **Cek Kualitas Sinyal Wi-Fi (RSSI):**
   Pastikan lokasi ESP32 mendapat sinyal Wi-Fi yang memadai (RSSI > -70 dBm).
3. **Cek Server Streaming:**
   Pastikan server local (`main.py`) dapat menyajikan file MP3 dengan latency rendah tanpa hambatan *bandwidth*.

---

### 🔁 2. Suara Ke-Ulang Ulang / Looping Terus Menerus

**Penyebab:**
1. **Looping Status `isRunning()` Tidak Direset:**
   Fungsi `mp3->loop()` dipanggil secara terus-menerus di fungsi `loop()` utama ESP32 tanpa memeriksa apakah *stream* audio sudah selesai (`EOF` / *End of File*).
2. **Koneksi Re-Connect Wi-Fi / Server:**
   Jika Wi-Fi putus-nyambung (*reconnect*), event listener WebSocket atau HTTP mungkin memicu pemutaran ulang URL MP3 yang sama secara tidak sengaja.

**Solusi & Handling Kode:**
* Pastikan logika pemutaran menghentikan stream setelah file selesai diputar:
  ```cpp
  if (mp3->isRunning()) {
      if (!mp3->loop()) {
          mp3->stop();
          // Hentikan dan bersihkan resource
          delete buff;
          delete file;
          delete mp3;
          Serial.println("Audio selesai diputar.");
      }
  }
  ```
* Jangan memicu `playAudio(url)` kembali kecuali ada event `tts_ready` baru dari WebSocket server.

---

### 🔇 3. Tidak Ada Suara Sama Sekali / Distorsi Parah

**Penyebab & Ceklis:**
1. **Wiring I2S MAX98357A:**
   * **BCLK** $
ightarrow$ Pin **26**
   * **LRC / WCLK** $
ightarrow$ Pin **25**
   * **DOUT** $
ightarrow$ Pin **27**
   * **GND & VCC** $
ightarrow$ Sesuai tegangan (5V/3.3V)
2. **Error Compile:**
   Jika muncul `AudioFileSourceHTTPStream.h: No such file or directory`:
   * Buka Arduino IDE $
ightarrow$ **Tools** $
ightarrow$ **Manage Libraries...**
   * Cari dan install library **ESP8266Audio** oleh **earlephilhower**.

---

## 📌 Pinout Ringkas (MAX98357A ke ESP32 DevKit V1)

| MAX98357A Pin | ESP32 Pin | Fungsi |
| :--- | :--- | :--- |
| **BCLK** | GPIO 26 | Bit Clock |
| **LRC** | GPIO 25 | Left/Right Clock (WCLK) |
| **DIN / DOUT** | GPIO 27 | Data Out |
| **GND** | GND | Ground |
| **VIN / VCC** | 5V / 3.3V | Power Supply |