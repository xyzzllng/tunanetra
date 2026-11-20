/*
 * Spider Sense - ESP32 Quick Button Remote + Ultrasonic Distance Sensor
 *                + TTS Audio Playback (via esp8266Audio)
 * ===========================================================================
 * VERSI GABUNGAN (Opsi A):
 *   Kode ini mendukung DUA JALUR TTS SEKALIGUS, supaya kompatibel dengan
 *   backend/web yang MASIH mengirim event lama "tts_ready" (URL MP3) DAN
 *   siap juga menerima push PCM langsung lewat POST /tts/play kalau
 *   backend-nya nanti diupdate ke skema baru.
 *
 *       1) BUZZER (2 pin, differential drive) -> HANYA untuk semua bunyi
 *          "beep" internal: peringatan ultrasonik, konfirmasi tombol,
 *          paired/quick_ack/error dari WebSocket, perintah "beep" serial.
 *          Buzzer TIDAK menyentuh I2S sama sekali.
 *
 *       2) SPEAKER I2S (MAX98357A, via AudioOutputI2S) -> dipakai oleh
 *          DUA sumber TTS yang berbeda, TAPI SALING MENGUNCI (mutex
 *          sederhana lewat flag `audioBusy`) supaya tidak rebutan I2S
 *          di saat bersamaan:
 *
 *          a) JALUR LAMA - "tts_ready" (pull):
 *             Backend kirim {"type":"tts_ready","url":"..."} lewat
 *             WebSocket -> ESP32 sendiri GET file MP3 dari URL itu
 *             (AudioFileSourceHTTPStream + AudioGeneratorMP3), lalu
 *             di-decode & diputar ke speaker. NON-BLOCKING: di-drive
 *             oleh mp3->loop() yang dipanggil tiap iterasi loop().
 *
 *          b) JALUR BARU - POST /tts/play (push):
 *             Komputer POST raw PCM 16-bit/44100Hz/stereo langsung ke
 *             ESP32. BLOCKING selama request berlangsung (streamPcmToI2S).
 *
 *          Kalau salah satu sedang jalan, yang lain akan ditolak/diabaikan
 *          sampai yang pertama selesai - lihat komentar di masing-masing
 *          fungsi handler.
 *
 * Library yang perlu di-install di Arduino Library Manager:
 *   - ESP8266Audio        (by Earle F. Philhower, III)
 *       -> AudioOutputI2S.h, AudioFileSourceHTTPStream.h,
 *          AudioFileSourceBuffer.h, AudioGeneratorMP3.h
 *   - WebSockets           (by Markus Sattler)   -> WebSocketsClient.h
 *   - ArduinoJson
 *   - (WebServer.h & WiFi.h sudah bawaan ESP32 core)
 * ===========================================================================
 */

#include <WiFi.h>
#include <WebServer.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "secrets.h"

#include <AudioOutputI2S.h>
#include <AudioFileSourceHTTPStream.h>
#include <AudioFileSourceBuffer.h>
#include <AudioGeneratorMP3.h>

// ============================== KONFIGURASI ===============================

// --- Tombol ---
#define BTN_1_PIN            21
#define BTN_2_PIN            22
#define BTN_3_PIN            23

// --- Sensor Ultrasonik HC-SR04 ---
#define ULTRASONIC_TRIG_PIN  5
#define ULTRASONIC_ECHO_PIN  18

#define DISTANCE_THRESHOLD_CM 100.0f
#define ULTRASONIC_INTERVAL_MS 100
#define SOUND_SPEED 0.034f

// --- BUZZER (differential drive, 2 pin) ---
#define BUZZER_PIN            4
#define BUZZER_PIN_B          15
#define BUZZER_TIMER_NUM      0

// --- I2S (MAX98357A) ---
#define I2S_BCLK             26
#define I2S_LRC               25
#define I2S_DOUT              27

// Sample rate & format untuk jalur PUSH (POST /tts/play).
// HARUS sama dengan yang dipakai tools/esp32_pcm_push.py.
#define AUDIO_SAMPLE_RATE     44100
#define AUDIO_BITS            16
#define AUDIO_CHANNELS        2      // stereo

#define BEEP_AMPLITUDE        8000
#define BEEP_MIN_AMPLITUDE    1800

// --- LED status ---
#define ENABLE_STATUS_LED    1
#define STATUS_LED_PIN       2

// --- Timing ---
#define DEBOUNCE_MS           40
#define REPEAT_GUARD_MS       600

#define WIFI_RETRY_MS         5000
#define WS_RECONNECT_MS       3000

// --- HTTP server (endpoint TTS jalur push) ---
#ifndef SERVER_LISTEN_PORT
#define SERVER_LISTEN_PORT     8080
#endif

#define TTS_READ_CHUNK_BYTES   1024

// --- Buffer streaming HTTP untuk jalur pull (MP3 dari URL) ---
#define HTTP_BUFFER_SIZE       4096

// ==========================================================================

WebSocketsClient webSocket;
WebServer httpServer(SERVER_LISTEN_PORT);

// Satu-satunya pemilik I2S, dipakai bergantian oleh JALUR PUSH (PCM) dan
// JALUR PULL (MP3 decode). writeSampleBlocking() (push) dan mp3->loop()
// (pull) sama-sama menulis ke objek yang sama ini, tapi tidak akan
// berjalan bersamaan karena dijaga flag audioBusy di bawah.
AudioOutputI2S audioOut;

// --- State jalur PULL (tts_ready -> MP3 dari URL) ---
AudioGeneratorMP3 *mp3 = nullptr;
AudioFileSourceHTTPStream *fileSource = nullptr;
AudioFileSourceBuffer *bufferedSource = nullptr;
bool isPlayingUrlAudio = false;
String lastTtsRequestId = "";

// --- State jalur PUSH (POST /tts/play -> raw PCM) ---
volatile bool ttsPlaying = false;

// --- Mutex sederhana antara jalur pull & push, supaya tidak rebutan I2S ---
// true = salah satu jalur audio (pull ATAU push) sedang aktif.
bool audioBusy() {
  return isPlayingUrlAudio || ttsPlaying;
}

// Nama event tombol
const char *BUTTON_EVENT[3] = {
  "object_detection",
  "expression_detection",
  "text_detection"
};

const int BUTTON_PIN[3] = {
  BTN_1_PIN,
  BTN_2_PIN,
  BTN_3_PIN
};

const int BUTTON_TONE[3] = {
  800,
  1000,
  600
};

int stableState[3]  = { HIGH, HIGH, HIGH };
int lastRawState[3] = { HIGH, HIGH, HIGH };
unsigned long lastChange[3] = { 0, 0, 0 };
unsigned long lastSent[3]   = { 0, 0, 0 };

unsigned long lastUltrasonicReadMs = 0;
unsigned long lastObstacleBeepMs = 0;

bool wsConnected = false;
bool paired = false;

unsigned long lastWifiAttemptMs = 0;

hw_timer_t *buzzerTimer = NULL;
volatile bool buzzerActive = false;
volatile bool buzzerPinState = false;
unsigned long buzzerEndMs = 0;


// ==========================================================================
// BUZZER - DIFFERENTIAL DRIVE 2-PIN VIA HARDWARE TIMER (NON-BLOCKING)
// ==========================================================================

void IRAM_ATTR onBuzzerTimer() {
  if (!buzzerActive) {
    return;
  }
  buzzerPinState = !buzzerPinState;
  digitalWrite(BUZZER_PIN,   buzzerPinState ? HIGH : LOW);
  digitalWrite(BUZZER_PIN_B, buzzerPinState ? LOW  : HIGH);
}

void setupBuzzer() {
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(BUZZER_PIN_B, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
  digitalWrite(BUZZER_PIN_B, LOW);

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  buzzerTimer = timerBegin(1000000);
  timerAttachInterrupt(buzzerTimer, &onBuzzerTimer);
  timerAlarm(buzzerTimer, 500, true, 0);
#else
  buzzerTimer = timerBegin(BUZZER_TIMER_NUM, 80, true);
  timerAttachInterrupt(buzzerTimer, &onBuzzerTimer, true);
  timerAlarmWrite(buzzerTimer, 500, true);
  timerAlarmEnable(buzzerTimer);
#endif
}

void updateBuzzer() {
  if (!buzzerActive) {
    return;
  }
  if ((long)(millis() - buzzerEndMs) >= 0) {
    buzzerActive = false;
    digitalWrite(BUZZER_PIN, LOW);
    digitalWrite(BUZZER_PIN_B, LOW);
  }
}


// ==========================================================================
// AUDIO I2S - INISIALISASI (dipakai bersama oleh jalur pull & push)
// ==========================================================================

void setupAudioOutput() {
  audioOut.SetPinout(I2S_BCLK, I2S_LRC, I2S_DOUT);
  audioOut.SetBuffers(8, 512);
  audioOut.SetGain(1.0f);
  audioOut.SetRate(AUDIO_SAMPLE_RATE);
  audioOut.SetChannels(AUDIO_CHANNELS);
  audioOut.begin();
  flushI2SSilence();
}

void writeSampleBlocking(int16_t sample[2]) {
  unsigned long start = millis();
  while (!audioOut.ConsumeSample(sample)) {
    if (millis() - start > 200) {
      return;
    }
    delayMicroseconds(50);
  }
}

void flushI2SSilence() {
  int16_t silence[2] = { 0, 0 };
  for (int i = 0; i < 1536; i++) {
    writeSampleBlocking(silence);
  }
}


// ==========================================================================
// PLAY BEEP - lewat buzzer terpisah, tidak menyentuh I2S sama sekali
// ==========================================================================

void playBeep(
  int frequency,
  int duration_ms,
  int amplitude = BEEP_AMPLITUDE
) {
  (void)amplitude;

  if (frequency <= 0 || duration_ms <= 0) {
    return;
  }

  unsigned long halfPeriodUs = 500000UL / (unsigned long)frequency;
  if (halfPeriodUs < 1) {
    halfPeriodUs = 1;
  }

#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  timerAlarm(buzzerTimer, halfPeriodUs, true, 0);
#else
  timerAlarmWrite(buzzerTimer, halfPeriodUs, true);
#endif

  buzzerPinState = false;
  buzzerActive = true;
  buzzerEndMs = millis() + (unsigned long)duration_ms;
}


// ==========================================================================
// JALUR PULL - "tts_ready" (URL MP3) - DIAMBIL DARI KODE LAMA
// ==========================================================================
//
// Backend kirim {"type":"tts_ready","url":"...","request_id":"..."} lewat
// WebSocket. ESP32 GET file MP3 dari URL itu, decode, dan putar ke
// audioOut yang sama dipakai jalur push. Non-blocking: di-drive oleh
// mp3->loop() yang dipanggil tiap iterasi loop() utama.
//
// ==========================================================================

// ==========================================================================
// FIX MEMORY LEAK & RE-INIT I2S
// ==========================================================================

// ==========================================================================
// FIX CRASH / SW_CPU_RESET ON SECOND TTS PLAYBACK
// ==========================================================================

void stopUrlAudio() {
  isPlayingUrlAudio = false;

  if (mp3 != nullptr) {
    if (mp3->isRunning()) {
      mp3->stop();
    }
    delete mp3;
    mp3 = nullptr;
  }

  if (bufferedSource != nullptr) {
    delete bufferedSource;
    bufferedSource = nullptr;
  }

  if (fileSource != nullptr) {
    fileSource->close(); // Tutup koneksi HTTP aktif dulu
    delete fileSource;
    fileSource = nullptr;
  }

  // Beri jeda 50ms agar stack/heap benar-benar bersih dan socket HTTP terlepas
  delay(50);
  yield();

  Serial.print("{\"event\":\"log\",\"message\":\"tts (url) dihentikan. Free Heap: ");
  Serial.print(ESP.getFreeHeap());
  Serial.println(" byte\"}");
}

void startUrlTtsPlayback(const String &fullUrl) {

  if (ttsPlaying) {
    Serial.println("{\"event\":\"log\",\"message\":\"tts (url) ditunda: jalur push sedang aktif\"}");
    return;
  }

  // Jika sedang memutar, bersihkan total dulu
  stopUrlAudio();

  // Beri napas ke Watchdog Timer sebelum alokasi HTTP berat
  yield();
  delay(100);

  Serial.print("{\"event\":\"log\",\"message\":\"mencoba koneksi tts ke: ");
  Serial.print(fullUrl);
  Serial.println("\"}");

  // Alokasi file source HTTP
  fileSource = new AudioFileSourceHTTPStream();
  if (fileSource == nullptr) {
    Serial.println("{\"event\":\"log\",\"message\":\"Gagal buat fileSource (Out of Memory)\"}");
    return;
  }

  // Buka URL
  if (!fileSource->open(fullUrl.c_str())) {
    Serial.println("{\"event\":\"log\",\"message\":\"Gagal membuka URL HTTP TTS\"}");
    stopUrlAudio();
    return;
  }

  // Alokasi Buffer HTTP
  bufferedSource = new AudioFileSourceBuffer(fileSource, HTTP_BUFFER_SIZE);
  if (bufferedSource == nullptr) {
    Serial.println("{\"event\":\"log\",\"message\":\"Gagal buat bufferedSource\"}");
    stopUrlAudio();
    return;
  }

  // Alokasi MP3 Decoder
  mp3 = new AudioGeneratorMP3();
  if (mp3 == nullptr) {
    Serial.println("{\"event\":\"log\",\"message\":\"Gagal buat MP3 Decoder\"}");
    stopUrlAudio();
    return;
  }

  // Cek free heap sebelum start decode
  if (ESP.getFreeHeap() < 30000) {
    Serial.print("{\"event\":\"log\",\"message\":\"Heap kritis sebelum mp3->begin: ");
    Serial.print(ESP.getFreeHeap());
    Serial.println(" byte\"}");
    stopUrlAudio();
    return;
  }

  // Jalankan decoder
  isPlayingUrlAudio = mp3->begin(bufferedSource, &audioOut);

  if (isPlayingUrlAudio) {
    Serial.print("{\"event\":\"log\",\"message\":\"berhasil memutar tts (free heap=");
    Serial.print(ESP.getFreeHeap());
    Serial.println(" byte)\"}");
  } else {
    Serial.println("{\"event\":\"log\",\"message\":\"gagal mp3->begin()\"}");
    stopUrlAudio();
  }
}

// Dipanggil tiap iterasi loop()
void updateUrlAudio() {
  if (!isPlayingUrlAudio || mp3 == nullptr) {
    return;
  }

  if (mp3->isRunning()) {
    // Jalankan loop decode
    if (!mp3->loop()) {
      stopUrlAudio();
    }
  } else {
    stopUrlAudio();
  }
}


// ==========================================================================
// JALUR PUSH - POST /tts/play (raw PCM) - dari kode baru, TIDAK diubah
// ==========================================================================

bool checkAuthToken() {
  if (strlen(ESP32_TOKEN) == 0) {
    return true;
  }
  String token = httpServer.header("X-Auth-Token");
  return token.equals(ESP32_TOKEN);
}

// Dipanggil berkala DARI DALAM streamPcmToI2S() supaya fitur lain (buzzer,
// ultrasonik, tombol, websocket) tetap responsif selagi handleTtsPlay()
// blocking menunggu/menulis data audio. TIDAK memanggil httpServer /
// ensureWiFi() di sini - itu bisa bikin re-entrancy / mengacaukan koneksi
// yang sedang dipakai untuk streaming.
void serviceDuringPlayback() {
  webSocket.loop();
  handleButtons();
  handleUltrasonic();
  updateBuzzer();
}

void streamPcmToI2S(WiFiClient &client, size_t contentLength) {

  uint8_t buffer[TTS_READ_CHUNK_BYTES];
  size_t remaining = contentLength;

  uint8_t carry[4];
  size_t carryLen = 0;

  unsigned long lastByteMs = millis();
  unsigned long lastServiceMs = millis();

  while (remaining > 0) {

    // Jangan panggil setiap iterasi (bisa terlalu sering saat data lancar) -
    // cukup tiap ~20ms, cukup untuk menjaga ultrasonik (interval 100ms) dan
    // buzzer tetap responsif tanpa mengganggu throughput streaming.
    if (millis() - lastServiceMs >= 20) {
      lastServiceMs = millis();
      serviceDuringPlayback();
    }

    size_t want = min(sizeof(buffer), remaining);
    int avail = client.available();

    if (avail <= 0) {
      if (millis() - lastByteMs > 5000) {
        break;
      }
      delay(1);
      continue;
    }

    lastByteMs = millis();

    int got = client.read(buffer, min((size_t)avail, want));
    if (got <= 0) {
      continue;
    }

    remaining -= got;

    size_t offset = 0;

    if (carryLen > 0) {
      while (carryLen < 4 && offset < (size_t)got) {
        carry[carryLen++] = buffer[offset++];
      }
      if (carryLen == 4) {
        int16_t l = (int16_t)(carry[0] | (carry[1] << 8));
        int16_t r = (int16_t)(carry[2] | (carry[3] << 8));
        int16_t sample[2] = { l, r };
        writeSampleBlocking(sample);
        carryLen = 0;
      }
    }

    size_t frames = (got - offset) / 4;

    for (size_t f = 0; f < frames; f++) {
      size_t p = offset + f * 4;
      int16_t l = (int16_t)(buffer[p]     | (buffer[p + 1] << 8));
      int16_t r = (int16_t)(buffer[p + 2] | (buffer[p + 3] << 8));
      int16_t sample[2] = { l, r };
      writeSampleBlocking(sample);
    }

    size_t leftoverStart = offset + frames * 4;
    size_t leftoverCount = got - leftoverStart;

    if (leftoverCount > 0 && leftoverCount < 4) {
      memcpy(carry, buffer + leftoverStart, leftoverCount);
      carryLen = leftoverCount;
    }
  }
}

void handleTtsPlay() {

  if (!checkAuthToken()) {
    httpServer.send(401, "text/plain", "unauthorized");
    return;
  }

  // Jangan terima push PCM kalau jalur pull (MP3 dari URL) sedang aktif.
  if (isPlayingUrlAudio) {
    httpServer.send(409, "text/plain", "audio sibuk (url playback aktif)");
    return;
  }

  size_t contentLength = httpServer.clientContentLength();

  if (contentLength == 0) {
    httpServer.send(400, "text/plain", "audio kosong");
    return;
  }

  ttsPlaying = true;

  WiFiClient client = httpServer.client();
  streamPcmToI2S(client, contentLength);

  flushI2SSilence();

  ttsPlaying = false;

  httpServer.send(200, "text/plain", "ok");
}

void setupHttpServer() {

  const char *headerKeysToCollect[] = { "X-Auth-Token" };
  httpServer.collectHeaders(headerKeysToCollect, 1);

  httpServer.on("/tts/play", HTTP_POST, handleTtsPlay);

  httpServer.onNotFound([]() {
    httpServer.send(404, "text/plain", "not found");
  });

  httpServer.begin();
}


// ==========================================================================
// LED STATUS
// ==========================================================================

void setStatusLed(bool on) {
#if ENABLE_STATUS_LED
  digitalWrite(STATUS_LED_PIN, on ? HIGH : LOW);
#else
  (void)on;
#endif
}


// ==========================================================================
// ULTRASONIK
// ==========================================================================

float readDistanceCM() {
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(ULTRASONIC_TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(ULTRASONIC_TRIG_PIN, LOW);

  long duration = pulseIn(ULTRASONIC_ECHO_PIN, HIGH, 30000);

  if (duration == 0) {
    return 999.0f;
  }
  return (duration * SOUND_SPEED) / 2.0f;
}

int getVolumeFromDistance(float distance) {
  distance = constrain(distance, 20.0f, 100.0f);
  int volume = map((int)distance, 100, 20, BEEP_MIN_AMPLITUDE, BEEP_AMPLITUDE);
  return constrain(volume, BEEP_MIN_AMPLITUDE, BEEP_AMPLITUDE);
}

int getBeepInterval(float distance) {
  distance = constrain(distance, 20.0f, 100.0f);
  int interval = map((int)distance, 100, 20, 700, 100);
  return constrain(interval, 100, 700);
}

void handleUltrasonic() {

  unsigned long now = millis();

  if (now - lastUltrasonicReadMs < ULTRASONIC_INTERVAL_MS) {
    return;
  }
  lastUltrasonicReadMs = now;

  float distance = readDistanceCM();

  if (distance > 0.0f && distance <= DISTANCE_THRESHOLD_CM) {

    int beepInterval = getBeepInterval(distance);

    if (now - lastObstacleBeepMs >= beepInterval) {

      lastObstacleBeepMs = now;

      int amplitude = getVolumeFromDistance(distance);

      JsonDocument doc;
      doc["event"] = "obstacle_warning";
      doc["distance_cm"] = distance;
      doc["volume"] = amplitude;
      doc["beep_interval"] = beepInterval;
      doc["device_id"] = DEVICE_ID;

      serializeJson(doc, Serial);
      Serial.println();

      if (wsConnected) {
        doc["type"] = "quick_command";
        if (strlen(ESP32_TOKEN) > 0) {
          doc["token"] = ESP32_TOKEN;
        }
        String out;
        serializeJson(doc, out);
        webSocket.sendTXT(out);
      }

      playBeep(1200, 100, amplitude);
    }
  }
}


// ==========================================================================
// WI-FI
// ==========================================================================

const char *wifiStatusToText(wl_status_t status) {
  switch (status) {
    case WL_IDLE_STATUS:     return "IDLE_STATUS (belum mulai / baru reset)";
    case WL_NO_SSID_AVAIL:   return "NO_SSID_AVAIL (SSID TIDAK DITEMUKAN di scan)";
    case WL_SCAN_COMPLETED:  return "SCAN_COMPLETED";
    case WL_CONNECTED:       return "CONNECTED";
    case WL_CONNECT_FAILED:  return "CONNECT_FAILED (kemungkinan besar PASSWORD SALAH)";
    case WL_CONNECTION_LOST: return "CONNECTION_LOST";
    case WL_DISCONNECTED:    return "DISCONNECTED";
    default:                 return "UNKNOWN";
  }
}

void scanAndPrintNetworks() {

  Serial.println("{\"event\":\"log\",\"message\":\"scanning wifi terdekat...\"}");

  int n = WiFi.scanNetworks();

  if (n <= 0) {
    Serial.println("{\"event\":\"log\",\"message\":\"scan: TIDAK ADA jaringan 2.4GHz terlihat sama sekali\"}");
    return;
  }

  bool targetFound = false;

  for (int i = 0; i < n; i++) {

    String ssid = WiFi.SSID(i);
    int rssi = WiFi.RSSI(i);
    wifi_auth_mode_t enc = WiFi.encryptionType(i);

    const char *encText;
    switch (enc) {
      case WIFI_AUTH_OPEN:            encText = "OPEN"; break;
      case WIFI_AUTH_WEP:             encText = "WEP"; break;
      case WIFI_AUTH_WPA_PSK:         encText = "WPA_PSK"; break;
      case WIFI_AUTH_WPA2_PSK:        encText = "WPA2_PSK"; break;
      case WIFI_AUTH_WPA_WPA2_PSK:    encText = "WPA_WPA2_PSK"; break;
      case WIFI_AUTH_WPA2_ENTERPRISE: encText = "WPA2_ENTERPRISE (butuh login domain/RADIUS)"; break;
      case WIFI_AUTH_WPA3_PSK:        encText = "WPA3_PSK (ESP32 core lama kurang stabil di WPA3-only)"; break;
      case WIFI_AUTH_WPA2_WPA3_PSK:   encText = "WPA2_WPA3_PSK"; break;
      default:                        encText = "LAINNYA"; break;
    }

    if (ssid.equals(WIFI_SSID)) {
      targetFound = true;
    }

    Serial.print("{\"event\":\"log\",\"message\":\"scan[");
    Serial.print(i);
    Serial.print("]: ssid='");
    Serial.print(ssid);
    Serial.print("' rssi=");
    Serial.print(rssi);
    Serial.print("dBm enc=");
    Serial.print(encText);
    Serial.println("\"}");
  }

  if (!targetFound) {
    Serial.print("{\"event\":\"log\",\"message\":\"PERINGATAN: SSID '");
    Serial.print(WIFI_SSID);
    Serial.println("' TIDAK ADA di daftar hasil scan di atas!\"}");
  }

  WiFi.scanDelete();
}

void connectWiFi() {

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  // Matikan penyimpanan kredensial WiFi ke flash (persistent). Ini bukan
  // soal SSID/password (itu selalu dari secrets.h), tapi ESP-IDF kadang
  // membawa state internal aneh dari sesi sebelumnya kalau persistent
  // aktif, yang bisa berkontribusi ke error "cannot set config".
  WiFi.persistent(false);

  scanAndPrintNetworks();

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("{\"event\":\"log\",\"message\":\"menghubungkan wifi ke '");
  Serial.print(WIFI_SSID);
  Serial.println("'\"}");

  unsigned long start = millis();
  wl_status_t lastStatus = (wl_status_t)255;

  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {

    wl_status_t current = WiFi.status();

    if (current != lastStatus) {
      lastStatus = current;
      Serial.print("{\"event\":\"log\",\"message\":\"wifi status berubah -> ");
      Serial.print((int)current);
      Serial.print(" (");
      Serial.print(wifiStatusToText(current));
      Serial.println(")\"}");
    }

    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("{\"event\":\"log\",\"message\":\"wifi ok, ip=");
    Serial.print(WiFi.localIP());
    Serial.println("\"}");
  } else {
    Serial.print("{\"event\":\"log\",\"message\":\"wifi gagal, status akhir=");
    Serial.print(wifiStatusToText(WiFi.status()));
    Serial.println(", memakai jalur serial saja\"}");
  }

  // PENTING: tandai waktu percobaan ini SEKARANG, supaya ensureWiFi() di
  // loop() tidak langsung mencoba WiFi.begin() lagi begitu setup() selesai.
  // Tanpa ini, ensureWiFi() akan retry dalam hitungan milidetik setelah
  // percobaan yang baru saja gagal - sementara stack WiFi ESP-IDF masih
  // dalam proses membereskan percobaan sebelumnya (auto-reconnect
  // internal), sehingga WiFi.begin() berikutnya gagal dengan error
  // "sta is connecting, cannot set config".
  lastWifiAttemptMs = millis();
}

void ensureWiFi() {

  if (WiFi.status() == WL_CONNECTED) {
    return;
  }
  if (millis() - lastWifiAttemptMs < WIFI_RETRY_MS) {
    return;
  }

  lastWifiAttemptMs = millis();

  wsConnected = false;
  paired = false;

  // Beri jeda kecil antara disconnect() dan begin() lagi. Memanggil
  // keduanya balik-balik tanpa jeda adalah penyebab paling umum dari
  // error "sta is connecting, cannot set config" di ESP32 - driver
  // WiFi internal butuh waktu sesaat untuk benar-benar selesai
  // membatalkan percobaan sebelumnya sebelum menerima config baru.
  WiFi.disconnect(true);
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}


// ==========================================================================
// WEBSOCKET
// ==========================================================================

void registerDevice() {
  JsonDocument doc;
  doc["type"] = "register";
  doc["role"] = "esp32";
  doc["device_id"] = DEVICE_ID;

  String out;
  serializeJson(doc, out);
  webSocket.sendTXT(out);
}

void onWsEvent(WStype_t type, uint8_t *payload, size_t length) {

  switch (type) {

    case WStype_CONNECTED:
      wsConnected = true;
      Serial.println("{\"event\":\"log\",\"message\":\"websocket terhubung\"}");
      registerDevice();
      break;

    case WStype_DISCONNECTED:
      wsConnected = false;
      paired = false;
      Serial.println("{\"event\":\"log\",\"message\":\"websocket terputus, mencoba lagi\"}");
      break;

    case WStype_ERROR:
      // Payload biasanya berisi pesan error handshake HTTP (mis. status
      // code kalau server menolak) - berguna untuk membedakan "server
      // mati total" (langsung gagal konek TCP) vs "server hidup tapi
      // menolak handshake WS" (dapat respons HTTP tapi bukan 101).
      Serial.print("{\"event\":\"log\",\"message\":\"websocket error: ");
      if (length > 0) {
        Serial.write(payload, length);
      } else {
        Serial.print("(tidak ada detail dari library)");
      }
      Serial.println("\"}");
      break;

    case WStype_TEXT: {

      JsonDocument doc;

      if (deserializeJson(doc, payload, length)) {
        return;
      }

      String msgType = doc["type"] | "";

      // --- JALUR PULL: backend masih pakai skema lama (URL MP3) ---
      if (msgType == "tts_ready") {

        String url = doc["url"] | "";
        String requestId = doc["request_id"] | "";

        if (requestId.length() > 0 && requestId == lastTtsRequestId) {
          Serial.println("{\"event\":\"log\",\"message\":\"tts duplikat diabaikan\"}");
        } else if (url.length() > 0) {

          if (requestId.length() > 0) lastTtsRequestId = requestId;

          String fullUrl;
          if (url.startsWith("http://") || url.startsWith("https://")) {
            fullUrl = url;
          } else {
            fullUrl = String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + url;
          }

          startUrlTtsPlayback(fullUrl);
        }
      }

      else if (msgType == "registered") {
        const char *session = doc["paired_session"] | "";
        paired = (strlen(session) > 0);
      }

      else if (msgType == "paired") {
        paired = true;
        Serial.println("{\"event\":\"log\",\"message\":\"dipasangkan ke halaman web\"}");
        playBeep(1200, 90);
      }

      else if (msgType == "quick_ack") {
        int delivered = doc["delivered"] | 0;
        playBeep(delivered > 0 ? 1400 : 300, 80);
      }

      else if (msgType == "error") {
        playBeep(300, 120);
      }

      break;
    }

    default:
      break;
  }
}


// ==========================================================================
// PENGIRIMAN EVENT TOMBOL
// ==========================================================================

void sendButtonEvent(int index) {

  const char *eventName = BUTTON_EVENT[index];
  int buttonNumber = index + 1;

  JsonDocument serialDoc;
  serialDoc["event"] = eventName;
  serialDoc["button"] = buttonNumber;
  serialDoc["device_id"] = DEVICE_ID;

  serializeJson(serialDoc, Serial);
  Serial.println();

  if (wsConnected) {

    JsonDocument wsDoc;
    wsDoc["type"] = "quick_command";
    wsDoc["event"] = eventName;
    wsDoc["button"] = buttonNumber;
    wsDoc["device_id"] = DEVICE_ID;

    if (strlen(ESP32_TOKEN) > 0) {
      wsDoc["token"] = ESP32_TOKEN;
    }

    String out;
    serializeJson(wsDoc, out);
    webSocket.sendTXT(out);
  }

  playBeep(BUTTON_TONE[index], 60);
}


// ==========================================================================
// HANDLE BUTTON
// ==========================================================================

void handleButtons() {

  unsigned long now = millis();

  for (int i = 0; i < 3; i++) {

    int raw = digitalRead(BUTTON_PIN[i]);

    if (raw != lastRawState[i]) {
      lastRawState[i] = raw;
      lastChange[i] = now;
      continue;
    }

    if (now - lastChange[i] < DEBOUNCE_MS) {
      continue;
    }

    if (raw == stableState[i]) {
      continue;
    }

    stableState[i] = raw;

    if (raw == LOW) {
      if (now - lastSent[i] >= REPEAT_GUARD_MS) {
        lastSent[i] = now;
        sendButtonEvent(i);
      }
    }
  }
}


// ==========================================================================
// SERIAL COMMAND
// ==========================================================================

void handleIncomingSerial() {

  if (Serial.available() <= 0) {
    return;
  }

  String input = Serial.readStringUntil('\n');
  input.trim();

  if (input.length() == 0 || input[0] != '{') {
    return;
  }

  JsonDocument doc;

  if (deserializeJson(doc, input)) {
    return;
  }

  const char *command = doc["command"] | "";

  if (strcmp(command, "beep") == 0) {
    int freq = doc["freq"] | 800;
    int duration = doc["duration"] | 120;
    playBeep(freq, duration > 300 ? 300 : duration);
  }

  else if (strcmp(command, "ping") == 0) {
    Serial.println("{\"event\":\"log\",\"message\":\"pong\"}");
  }
}


// ==========================================================================
// SETUP
// ==========================================================================

void setup() {

  Serial.begin(115200);
  delay(200);

  for (int i = 0; i < 3; i++) {
    pinMode(BUTTON_PIN[i], INPUT_PULLUP);
    stableState[i] = digitalRead(BUTTON_PIN[i]);
    lastRawState[i] = stableState[i];
  }

  pinMode(ULTRASONIC_TRIG_PIN, OUTPUT);
  pinMode(ULTRASONIC_ECHO_PIN, INPUT);

#if ENABLE_STATUS_LED
  pinMode(STATUS_LED_PIN, OUTPUT);
#endif

  setupBuzzer();
  setupAudioOutput();

  connectWiFi();

  setupHttpServer();

  webSocket.begin(SERVER_HOST, SERVER_PORT, "/ws");
  webSocket.onEvent(onWsEvent);
  webSocket.setReconnectInterval(WS_RECONNECT_MS);
  webSocket.enableHeartbeat(15000, 3000, 2);

  // Supaya gampang diagnosis kalau WS gagal connect terus-menerus - cek
  // apakah IP/port ini memang IP backend yang sedang aktif SEKARANG
  // (bisa berubah kalau laptop server reconnect WiFi/dapat IP baru).
  Serial.print("{\"event\":\"log\",\"message\":\"mencoba websocket ke ws://");
  Serial.print(SERVER_HOST);
  Serial.print(":");
  Serial.print(SERVER_PORT);
  Serial.println("/ws\"}");

  playBeep(1000, 90);
  delay(120);
  playBeep(1500, 120);
  delay(150);

  Serial.println("{\"event\":\"log\",\"message\":\"siap: 1=objek 2=ekspresi 3=tulisan + ultrasonik(buzzer) + tts(url & push) aktif\"}");
  Serial.print("{\"event\":\"log\",\"message\":\"free heap awal=");
  Serial.print(ESP.getFreeHeap());
  Serial.println(" byte\"}");
}


// ==========================================================================
// LOOP
// ==========================================================================

void loop() {

  ensureWiFi();

  webSocket.loop();
  httpServer.handleClient();

  handleButtons();
  handleUltrasonic();
  handleIncomingSerial();
  updateBuzzer();

  // Drive decode MP3 jalur pull (tts_ready)
  updateUrlAudio();

  setStatusLed(wsConnected);

  // PENTING: Tambahkan yield() untuk memberi makan Watchdog Timer!
  yield(); 
  delay(2);
}