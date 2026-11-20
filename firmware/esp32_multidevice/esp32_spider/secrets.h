#pragma once

// Salin file ini menjadi secrets.h, lalu isi dengan konfigurasi lokal Anda.
// Jangan commit secrets.h ke Git (.gitignore sudah menutupinya).

#define WIFI_SSID       "suami hutao2"
#define WIFI_PASSWORD   "Hutao_Husban"

// IP LAN komputer yang menjalankan main.py.
// Lihat dengan `ipconfig` (Windows) atau `hostname -I` (Linux).
// JANGAN pakai localhost atau 127.0.0.1 - alamat itu menunjuk ke ESP32 sendiri.
#define SERVER_HOST     "192.168.137.1"
#define SERVER_PORT     5000

// ID unik alat ini. Ketikkan nilai yang sama di kolom "ID perangkat" pada
// halaman web, lalu tekan "Hubungkan Perangkat", supaya tombol fisik ini
// hanya mengendalikan HP/laptop yang dipasangkan dengannya.
#define DEVICE_ID       "esp32-A1"

// Isi bila ESP32_SHARED_TOKEN di .env server juga diisi. Kosongkan bila tidak.
#define ESP32_TOKEN     "KelomangMalam251"

// Nama Bluetooth TWS/headphone tujuan - HARUS PERSIS SAMA (termasuk huruf
// besar/kecil & spasi) dengan nama yang muncul saat kamu scan Bluetooth
// dari HP, mis. "TH20". ESP32 akan mencari perangkat dengan nama ini lalu
// connect sebagai sumber audio (A2DP source), seperti HP yang connect ke
// earphone-mu.
#define TWS_DEVICE_NAME  "TH20"