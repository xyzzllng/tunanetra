> **Arsip.** Dokumen sejarah sebelum migrasi ke ESP32-CAM; path dan alur (browser/webcam) merujuk tata letak lama. Lihat `docs/MIGRASI.md` untuk pemetaan file lama → baru.

# ESP32 sebagai remote controller 3 tombol Quick Command

Tiga tombol fisik pada ESP32 menekan tombol yang sudah ada di halaman web:

| Tombol ESP32 | Event | Mode internal | Tombol UI yang dipicu |
|---|---|---|---|
| 1 | `object_detection` | `objek` | Deteksi Objek |
| 2 | `expression_detection` | `ekspresi` | Deteksi Ekspresi |
| 3 | `text_detection` | `baca` | Deteksi Tulisan/Teks |

ESP32 **tidak** menjalankan AI. Ia hanya mengirim event. Seluruh deteksi tetap
dikerjakan Python + browser lewat `POST /api/quick` yang sudah ada.

---

## 1. Kenapa jalurnya lewat browser

Pada default `SERVER_MODE=multi`, kamera dimiliki **browser** (`getUserMedia`),
bukan Python. Jadi Python tidak bisa memotret sendiri saat tombol ditekan.
Jalur yang benar:

```
Tombol fisik ESP32
      ↓  WebSocket /ws  (atau POST /api/esp32/event)
Server Python: services/esp32_events.py → dispatch()
      ↓  SSE /api/esp32/stream
Browser: handleEsp32Quick() → runQuick(mode)
      ↓  POST /api/quick   ← endpoint lama, tidak diubah
Deteksi berjalan, hasil + TTS tampil seperti biasa
```

`runQuick(mode)` adalah **fungsi yang sama persis** dengan yang dipanggil
`addEventListener("click", ...)` pada tombol UI. Tidak ada logika deteksi yang
ditulis ulang, dan status/antrean/busy-guard yang sudah ada tetap berlaku.

Jalur ini juga dipakai pada `SERVER_MODE=single` (kamera dibuka Python), supaya
frontend punya satu code path dan tampilan UI tetap sinkron dengan tombol fisik.

---

## 2. Pilihan transport dan alasannya

**ESP32 → Server: WebSocket `/ws`.**
Project sudah punya endpoint ini beserta registrasi `role: "esp32"` dan pairing
`device_id ↔ session_id`. Koneksinya persisten, jadi latency tekan-ke-deteksi
rendah dan **tidak ada polling** dari ESP32. Firmware juga tetap mengirim baris
JSON ke Serial, sehingga jalur USB bisa dipakai sebagai cadangan.

**Server → Browser: Server-Sent Events `/api/esp32/stream`.**
SSE dipilih karena berjalan di HTTP biasa tanpa dependensi baru, searah
(server→browser sudah cukup), otomatis reconnect, dan bekerja **sama** di mode
`multi` maupun `single` — `server.py` tidak punya WebSocket sama sekali.
Browser membuka satu koneksi lalu menunggu; ini bukan polling.

**Alternatif `POST /api/esp32/event`** tetap disediakan untuk pengujian dengan
`curl`/tool, dan dipakai oleh bridge serial.

---

## 3. File yang dibuat / diubah

### Baru
| File | Isi |
|---|---|
| `services/esp32_events.py` | Hub event: pemetaan tombol, debounce server, routing ke session, SSE, `/api/esp32/*` |
| `esp32_quickbutton/esp32_quickbutton.ino` | Firmware hasil penggabungan `buttonconfig.ino` + Wi-Fi/WebSocket/reconnect |
| `esp32_quickbutton/secrets.h.example` | Template Wi-Fi, IP server, `DEVICE_ID` |
| `tools/check_esp32_button.py` | Uji seluruh jalur tanpa perangkat keras |
| `tests/test_esp32_frontend.cjs` | Bukti event ESP32 memanggil `runQuick` yang sama |
| `docs/ESP32_QUICK_BUTTON.md` | Dokumen ini |

### Diubah (semuanya bersifat menambah, bukan mengganti)
| File | Perubahan |
|---|---|
| `app_utils/esp32_bridge.py` | Ditulis ulang: reconnect otomatis, deteksi port, `send_command()` yang sebelumnya tidak ada, penulisan thread-safe, pyserial opsional |
| `main.py` | Menghapus kode mati di bawah `if __name__` yang tidak pernah berjalan |
| `config/settings.py` | Tambah blok konfigurasi `esp32_*` |
| `server_multidevice.py` | Tambah `quick_command` pada handler `/ws`, pasang hub + bridge serial |
| `server.py` | Pasang hub + bridge serial (mode single) |
| `web/app.js` | Klien SSE + `handleEsp32Quick()` → `runQuick()` |
| `web/index.html`, `web/style.css` | Panel status/log tombol fisik |
| `esp32_multidevice/esp32_multidevice.ino` | Tambah 3 tombol Quick Command pada GPIO bebas |
| `.env.example`, `requirements.txt`, `.gitignore` | Konfigurasi baru + `pyserial` |

---

## 4. Wiring GPIO

### Tombol — `esp32_quickbutton.ino`

Ketiganya `INPUT_PULLUP` aktif LOW, jadi **tidak perlu resistor eksternal**.

```
Tombol 1 (Deteksi Objek)    : kaki A → GPIO 4    kaki B → GND
Tombol 2 (Deteksi Ekspresi) : kaki A → GPIO 18   kaki B → GND
Tombol 3 (Deteksi Tulisan)  : kaki A → GPIO 19   kaki B → GND
```

```
   ESP32                     Push button (4 kaki)
   GPIO 4  ──────────────────┤ A1    A2 ├── (tidak dipakai)
                             │  \____/  │
   GND     ──────────────────┤ B1    B2 ├── (tidak dipakai)
```

Pada tombol 4 kaki, A1–A2 dan B1–B2 sudah tersambung di dalam. Pakai satu kaki
dari pasangan kiri dan satu dari pasangan kanan; kalau tombol terbaca terus
ditekan, putar tombol 90°.

### Speaker MAX98357A (opsional, untuk beep umpan balik)

```
BCLK → GPIO 26      LRC → GPIO 25      DIN → GPIO 22
VIN  → 5V (atau 3V3)                   GND → GND
```

Belum punya speaker? Set `#define ENABLE_BEEP 0` di firmware — semua fungsi
tombol tetap jalan.

### Kalau memakai `esp32_multidevice.ino` (firmware lengkap)

GPIO 4, 18, dan 19 di sana sudah dipakai push-to-talk, TRIG ultrasonik, dan
toggle. Karena itu tiga tombol Quick Command memakai pin bebas:

```
Tombol 1 → GPIO 32    Tombol 2 → GPIO 33    Tombol 3 → GPIO 27
```

Perlu `secrets.h` di folder tersebut, dan tombol hanya aktif saat toggle ON.

---

## 5. Konfigurasi

### `.env` (komputer)

```dotenv
ESP32_SHARED_TOKEN=
ESP32_EVENT_COOLDOWN_MS=700
ESP32_BROADCAST_FALLBACK=true

# Hanya bila memakai kabel USB, bukan Wi-Fi:
ESP32_SERIAL_ENABLED=false
ESP32_SERIAL_PORT=auto
```

`ESP32_BROADCAST_FALLBACK=true` membuat event diteruskan ke semua halaman web
yang terbuka bila ESP32 belum dipasangkan. Praktis untuk satu pengguna. Set
`false` bila ada beberapa pengguna serentak, lalu pasangkan tiap alat lewat
kolom "ID perangkat" di halaman web.

### `secrets.h` (ESP32)

```bash
cp esp32_quickbutton/secrets.h.example esp32_quickbutton/secrets.h
```

Isi `WIFI_SSID`, `WIFI_PASSWORD`, dan `SERVER_HOST` dengan **IP LAN komputer**
(`ipconfig` di Windows, `hostname -I` di Linux). Jangan pakai `localhost` atau
`127.0.0.1` — dari sudut pandang ESP32, alamat itu menunjuk ke dirinya sendiri.

---

## 6. Cara menjalankan

### Python

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt      # pyserial ikut terpasang
python main.py
```

Buka `http://localhost:5000`. Panel **Tombol fisik ESP32** harus berbunyi
"Jalur tombol fisik siap". Halaman web wajib terbuka: browser yang memegang
kamera, jadi tanpa halaman terbuka event tombol tidak bisa memicu apa pun.

### ESP32

1. Arduino IDE → Library Manager, pasang **WebSockets** (Markus Sattler) dan
   **ArduinoJson** 6.x.
2. Board: **ESP32 Dev Module**.
3. Buka `esp32_quickbutton/esp32_quickbutton.ino`, pastikan `secrets.h` sudah ada.
4. Upload, lalu buka Serial Monitor pada **115200**.

Saat siap akan terlihat:

```
{"event":"log","message":"wifi ok, ip=192.168.1.55"}
{"event":"log","message":"websocket terhubung"}
{"event":"log","message":"siap: 1=objek 2=ekspresi 3=tulisan"}
```

### Pairing (opsional tapi disarankan)

Di halaman web, isi kolom **ID perangkat** dengan `DEVICE_ID` dari `secrets.h`
(mis. `esp32-A1`), tekan **Hubungkan Perangkat**. Setelah itu event tombol hanya
menuju halaman itu, dan log server menampilkan `paired`, bukan `broadcast`.

---

## 7. Langkah pengujian

### A. Tanpa perangkat keras

```bash
python tools/check_esp32_button.py --status     # berapa halaman web mendengarkan
python tools/check_esp32_button.py --button 1   # harus memicu Deteksi Objek
python tools/check_esp32_button.py --button 2   # Deteksi Ekspresi
python tools/check_esp32_button.py --button 3   # Deteksi Tulisan
python tools/check_esp32_button.py --all --delay 12
```

Dengan halaman web terbuka, tiap perintah harus menampilkan
`OK tombol N ... diteruskan ke 1 halaman`, dan hasil deteksi muncul di layar.
Kalau muncul `tidak ada halaman web yang mendengarkan`, halaman belum terbuka
atau SSE terputus.

Tes otomatis:

```bash
node tests/test_frontend.cjs          # tombol UI lama tetap utuh
node tests/test_esp32_frontend.cjs    # event ESP32 memicu runQuick yang sama
```

### B. Dengan perangkat keras

| # | Langkah | Hasil yang benar |
|---|---|---|
| 1 | Nyalakan ESP32 | Serial Monitor: `wifi ok` lalu `websocket terhubung`; beep ganda |
| 2 | Buka halaman web | Panel ESP32: "Jalur tombol fisik siap" |
| 3 | **Tekan tombol 1** | Log: `Tombol 1 → Deteksi Objek`; hasil objek + kotak muncul; TTS berbunyi |
| 4 | **Tekan tombol 2** | Hasil ekspresi wajah (arahkan ke wajah) |
| 5 | **Tekan tombol 3** | Hasil OCR (arahkan ke tulisan) |
| 6 | Tekan tombol 1 cepat 5× | Hanya **satu** deteksi berjalan — debounce firmware + cooldown server |
| 7 | Tekan tombol saat deteksi berjalan | Log: "diabaikan: proses sebelumnya belum selesai" |
| 8 | Klik tombol layar dengan mouse | Tetap berfungsi seperti sebelumnya |
| 9 | Matikan ESP32 | Panel: "terputus"; **tombol layar tetap jalan** |
| 10 | Nyalakan lagi ESP32 | Tersambung sendiri dalam ±3 detik tanpa restart Python |
| 11 | Matikan `python main.py`, tekan tombol | ESP32 tidak hang; reconnect otomatis setelah server hidup |

Verifikasi log server kapan saja:

```bash
curl http://localhost:5000/api/esp32/status
```

---

## 8. Masalah yang ditemukan pada project lama dan solusinya

**1. Kode ESP32 di `main.py` tidak pernah berjalan.**
`esp32 = ESP32Bridge(...)` dan `esp32.connect()` ditulis **di bawah**
`if __name__ == "__main__": main()`. Karena `app.run()` memblokir sampai server
mati, baris itu baru dieksekusi setelah server berhenti.
→ Kode mati dihapus; bridge sekarang dipasang di dalam modul server.

**2. `esp32.send_command()` dipanggil padahal tidak ada.**
`ESP32Bridge` lama hanya punya `send_beep()`, jadi baris itu akan melempar
`AttributeError`.
→ `send_command()` sekarang benar-benar ada; `send_beep()` dipertahankan
sebagai alias supaya kode lama tidak rusak.

**3. Port serial di-hardcode `COM11` dan tidak pernah reconnect.**
Sekali kabel dicabut, bridge mati permanen.
→ `ESP32_SERIAL_PORT=auto` mendeteksi CP210x/CH340/FTDI, dan thread supervisor
mencoba ulang tiap 3 detik.

**4. Serial saja tidak cocok untuk arsitektur project ini.**
Kamera ada di browser HP, sementara serial mengikat ESP32 ke komputer lewat
kabel — HP tidak bisa dijangkau.
→ WebSocket dijadikan jalur utama; serial tetap tersedia sebagai cadangan
untuk pemakaian satu komputer.

**5. Debounce lama di `buttonconfig.ino` memakai satu variabel untuk tiga tombol.**
Menekan tombol 1 memblokir tombol 2 dan 3 selama 200 ms, dan `digitalRead LOW`
di dalam loop mengirim berulang selama tombol ditahan.
→ Diganti deteksi tepi turun per tombol (debounce 40 ms + repeat guard 600 ms),
sehingga satu tekanan = satu event, dan tombol saling bebas.

**6. Tidak ada yang mendengarkan bila halaman web tertutup.**
Ini batas nyata, bukan bug: browser yang memegang kamera.
→ Server membalas `no_listener`, firmware membunyikan nada rendah, dan panel
web menjelaskannya. Halaman harus terbuka.

**7. `pyserial` belum ada di `requirements.txt`** padahal `esp32_bridge.py`
mengimpornya.
→ Ditambahkan, dan impornya dibuat opsional supaya server tetap jalan bila
pyserial belum terpasang.

---

## 9. Catatan keamanan

Server ini prototipe tanpa autentikasi. Pada jaringan bersama, isi
`ESP32_SHARED_TOKEN` di `.env` dan `ESP32_TOKEN` di `secrets.h` dengan nilai
yang sama agar `POST /api/esp32/event` tidak bisa dipicu sembarang perangkat.
Jangan mengekspos port 5000 langsung ke internet.
