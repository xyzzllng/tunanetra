/*
 * Spider Sense - ESP32 Quick Button Remote + Ultrasonic Distance Sensor
 *                + TTS Audio Playback (via Bluetooth A2DP push PCM)
 *                + Sensor Cahaya LDR (modul 3 pin, output DO) -> LED penerangan
 *
 * ===========================================================================
 * PERUBAHAN TERBARU (fix "Connection reset" pada POST /tts/play):
 *
 *   Sebelumnya endpoint /tts/play memakai WebServer.h. Library itu membaca
 *   SELURUH body POST ke RAM sebelum handler dipanggil. PCM 44.1kHz stereo
 *   16-bit = ~172 KB per detik audio, sedangkan heap ESP32 cuma puluhan KB,
 *   sehingga alokasi gagal dan koneksi di-reset (handler tidak pernah jalan).
 *
 *   Sekarang memakai WiFiServer mentah: header HTTP dibaca manual, lalu body
 *   PCM di-stream per potongan (TTS_READ_CHUNK_BYTES) langsung dari socket ke
 *   ring buffer Bluetooth. Pemakaian heap konstan, berapa pun panjang audionya.
 *   Kontrak ke backend TIDAK berubah (POST /tts/play, raw PCM, header
 *   X-Auth-Token). GET / dan path lain tetap dibalas 404 "not found".
 * ===========================================================================
 *
 * VERSI DIPANGKAS (untuk hemat heap & resource RTOS di ESP32 tanpa PSRAM):
 *
 *   - Jalur PULL "tts_ready" (MP3 dari URL, via ESP8266Audio) DIHAPUS TOTAL.
 *     Backend HARUS pakai jalur PUSH (POST /tts/play, raw PCM) saja.
 *
 *   - TWS_RING_BUFFER_FRAMES diperkecil dari 8192 -> 2048 frame (hemat
 *     ~24KB SRAM). Kalau audio kedengaran patah-patah/putus pas WiFi lagi
 *     sibuk, boleh dinaikkan lagi pelan-pelan (mis. 3072, 4096) SAMBIL
 *     pantau free heap di Serial Monitor.
 *
 *   - Retry WiFi & WebSocket dibuat lebih longgar (backoff).
 *
 *   - Kalau BT lagi proses connect/reconnect, retry WiFi ditunda dulu.
 *
 * SENSOR CAHAYA (LDR modul 3 pin: VCC, GND, DO):
 *   VCC -> 3V3, GND -> GND, DO -> GPIO 34. Batas gelap/terang diatur dengan
 *   memutar potensiometer biru di modul. LED penerangan di GPIO 26 (lewat
 *   resistor 220 ohm ke GND).
 *
 * Library yang perlu di-install di Arduino Library Manager:
 *   - WebSockets           (by Markus Sattler)   -> WebSocketsClient.h
 *   - ArduinoJson
 *   - ESP32-A2DP           (by pschatzmann)      -> BluetoothA2DPSource.h
 *       PENTING: butuh board ESP32 klasik dual-mode (WROOM-32/WROVER dst)
 *       yang punya Bluetooth Classic. TIDAK berfungsi di ESP32-S2/S3/C3/C6.
 *   - (WiFi.h sudah bawaan ESP32 core; WebServer.h TIDAK dipakai lagi)
 * ===========================================================================
 */

#include <WiFi.h>
#define WEBSOCKETS_TCP_TIMEOUT 800
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "secrets.h"

#include <BluetoothA2DPSource.h>

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

// --- Sensor Cahaya LDR (modul 3 pin, output digital DO) + LED penerangan ---
#define LDR_PIN               34     // pin DO modul
#define LIGHT_LED_PIN         26
#define LDR_INTERVAL_MS       100
#define LDR_DARK_LEVEL        HIGH   // level DO saat GELAP (kebanyakan modul: HIGH)
                                     // kalau LED nyala saat terang, ganti ke LOW
#define LDR_STABLE_MS         400    // status baru harus stabil segini lama
#define LDR_PRINT_INTERVAL_MS 500

// Nama Bluetooth persis dari earphone/TWS Anda. Override di secrets.h:
//   #define TWS_DEVICE_NAME "Nama TWS Anda"
#ifndef TWS_DEVICE_NAME
#define TWS_DEVICE_NAME       "TH20"
#endif

// DIPERKECIL dari 8192 -> 2048 frame untuk hemat SRAM (chip tanpa PSRAM).
// 2048 frame @44100Hz stereo 16-bit ~= 46ms bantalan.
#define TWS_RING_BUFFER_FRAMES 2048

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

// Retry WiFi dengan backoff: mulai dari WIFI_RETRY_MS_MIN, naik tiap gagal
// sampai maksimum WIFI_RETRY_MS_MAX.
#define WIFI_RETRY_MS_MIN      5000
#define WIFI_RETRY_MS_MAX      30000

#define WS_RECONNECT_MS       5000

// --- HTTP server (endpoint TTS jalur push) ---
#ifndef SERVER_LISTEN_PORT
#define SERVER_LISTEN_PORT     8080
#endif

#define TTS_READ_CHUNK_BYTES   1024

// Batas waktu membaca satu baris header HTTP.
#define HTTP_HEADER_TIMEOUT_MS 3000

// ==========================================================================

WebSocketsClient webSocket;
WiFiServer ttsServer(SERVER_LISTEN_PORT);

// ==========================================================================
// TWS AUDIO (Bluetooth A2DP Source)
// ==========================================================================
struct TwsStereoFrame {
  int16_t l;
  int16_t r;
};

class TwsAudioRingBuffer {
  public:
    bool push(int16_t sample[2]) {
      size_t nextHead = (head + 1) % TWS_RING_BUFFER_FRAMES;
      if (nextHead == tail) {
        return false;  // buffer penuh
      }
      buf[head].l = sample[0];
      buf[head].r = sample[1];
      head = nextHead;
      return true;
    }
    bool pop(int16_t &l, int16_t &r) {
      if (tail == head) {
        return false;  // buffer kosong (underrun)
      }
      l = buf[tail].l;
      r = buf[tail].r;
      tail = (tail + 1) % TWS_RING_BUFFER_FRAMES;
      return true;
    }
  private:
    TwsStereoFrame buf[TWS_RING_BUFFER_FRAMES];
    volatile size_t head = 0;
    volatile size_t tail = 0;
};

TwsAudioRingBuffer twsRingBuffer;

// Sink audio custom minimal.
class AudioOutputA2DPSink {
  public:
    void SetRate(int hz) { hertz = hz; }
    void SetChannels(int ch) { channels = ch; }
    void SetGain(float f) { twsGain = f; }
    bool ConsumeSample(int16_t sample[2]) {
      int16_t out[2];
      out[0] = (int16_t)constrain((int)((float)sample[0] * twsGain), -32768, 32767);
      out[1] = (int16_t)constrain((int)((float)sample[1] * twsGain), -32768, 32767);
      return twsRingBuffer.push(out);
    }
  private:
    int hertz = AUDIO_SAMPLE_RATE;
    int channels = AUDIO_CHANNELS;
    float twsGain = 1.0f;
};

AudioOutputA2DPSink audioOut;

// Menggunakan tipe "Frame" sesuai definisi asli library ESP32-A2DP
int32_t twsGetDataFrames(Frame *frame, int32_t frame_count) {
  for (int32_t i = 0; i < frame_count; i++) {
    int16_t l = 0, r = 0;
    twsRingBuffer.pop(l, r);
    frame[i].channel1 = l;
    frame[i].channel2 = r;
  }
  return frame_count;
}

// Status koneksi BT, dipakai untuk menunda retry WiFi saat BT lagi sibuk
// connect/reconnect - supaya tidak rebutan radio & resource RTOS.
volatile bool btConnecting = false;
volatile bool btConnected = false;

void onTwsConnectionStateChanged(esp_a2d_connection_state_t state, void *ptr) {
  JsonDocument doc;
  doc["event"] = "log";
  switch (state) {
    case ESP_A2D_CONNECTION_STATE_CONNECTED:
      doc["message"] = "tws bluetooth terhubung";
      btConnecting = false;
      btConnected = true;
      break;
    case ESP_A2D_CONNECTION_STATE_DISCONNECTED:
      doc["message"] = "tws bluetooth terputus, mencoba menyambung ulang";
      btConnecting = false;
      btConnected = false;
      break;
    case ESP_A2D_CONNECTION_STATE_CONNECTING:
      doc["message"] = "tws bluetooth sedang menyambung...";
      btConnecting = true;
      break;
    case ESP_A2D_CONNECTION_STATE_DISCONNECTING:
      doc["message"] = "tws bluetooth sedang memutus...";
      break;
    default:
      doc["message"] = "tws bluetooth status berubah";
      break;
  }
  serializeJson(doc, Serial);
  Serial.println();
}

BluetoothA2DPSource a2dpSource;

// --- State jalur PUSH (POST /tts/play -> raw PCM) ---
volatile bool ttsPlaying = false;

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
unsigned long lastLdrReadMs = 0;
bool lightLedOn = false;

bool wsConnected = false;
bool paired = false;

unsigned long lastWifiAttemptMs = 0;
unsigned long currentWifiRetryMs = WIFI_RETRY_MS_MIN;

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
// AUDIO BT - INISIALISASI
// ==========================================================================

void setupAudioOutput() {
  audioOut.SetRate(AUDIO_SAMPLE_RATE);
  audioOut.SetChannels(AUDIO_CHANNELS);
  audioOut.SetGain(1.0f);

  // ISI BUFFER AWAL DENGAN SILENCE (Mencegah TWS langsung putus karena data kosong)
  int16_t initSilence[2] = { 0, 0 };
  for (int i = 0; i < 512; i++) {
    twsRingBuffer.push(initSilence);
  }

  a2dpSource.set_auto_reconnect(true, false);
  a2dpSource.set_on_connection_state_changed(onTwsConnectionStateChanged);

  a2dpSource.start(TWS_DEVICE_NAME, twsGetDataFrames);

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
  for (int i = 0; i < 384; i++) {
    writeSampleBlocking(silence);
  }
}


// ==========================================================================
// PLAY BEEP - lewat buzzer terpisah, tidak menyentuh audio BT sama sekali
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
// JALUR PUSH - POST /tts/play (raw PCM), memakai WiFiServer mentah
// ==========================================================================

// Dipanggil berkala DARI DALAM streamPcmToI2S() supaya fitur lain (buzzer,
// ultrasonik, tombol, LDR, websocket) tetap responsif selagi handler
// blocking menunggu/menulis data audio.
void serviceDuringPlayback() {
  webSocket.loop();
  handleButtons();
  handleLdr();
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

    if (millis() - lastServiceMs >= 20) {
      lastServiceMs = millis();
      serviceDuringPlayback();
    }

    size_t want = min(sizeof(buffer), remaining);
    int avail = client.available();

    if (avail <= 0) {
      if (!client.connected() && client.available() <= 0) {
        break;  // klien sudah menutup koneksi, tidak akan ada data lagi
      }
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

// Baca satu baris HTTP (sampai '\n') dari socket. Return false kalau timeout
// atau koneksi putus sebelum baris lengkap.
bool readHttpLine(WiFiClient &c, String &line, unsigned long timeoutMs) {
  line = "";
  unsigned long t0 = millis();
  while (millis() - t0 < timeoutMs) {
    while (c.available()) {
      char ch = (char)c.read();
      if (ch == '\n') {
        line.trim();
        return true;
      }
      if (ch != '\r' && line.length() < 200) {
        line += ch;
      }
    }
    if (!c.connected()) {
      return false;
    }
    delay(1);
  }
  return false;
}

void sendSimple(WiFiClient &c, int code, const char *status, const char *msg) {
  c.printf("HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %u\r\nConnection: close\r\n\r\n%s",
           code, status, (unsigned)strlen(msg), msg);
  c.flush();
  c.stop();
}

void handleTtsClient(WiFiClient client) {

  String line;

  // Request line: "POST /tts/play HTTP/1.1"
  if (!readHttpLine(client, line, HTTP_HEADER_TIMEOUT_MS)) {
    client.stop();
    return;
  }

  bool isTtsPost = line.startsWith("POST /tts/play");
  size_t contentLength = 0;
  String token = "";

  // Header sampai baris kosong. Body PCM TETAP di socket (tidak di-buffer).
  while (true) {
    if (!readHttpLine(client, line, HTTP_HEADER_TIMEOUT_MS)) {
      client.stop();
      return;
    }
    if (line.length() == 0) {
      break;  // akhir header
    }
    int colon = line.indexOf(':');
    if (colon < 0) {
      continue;
    }
    String key = line.substring(0, colon);
    String val = line.substring(colon + 1);
    val.trim();
    if (key.equalsIgnoreCase("Content-Length")) {
      contentLength = (size_t)val.toInt();
    } else if (key.equalsIgnoreCase("X-Auth-Token")) {
      token = val;
    }
  }

  if (!isTtsPost) {
    sendSimple(client, 404, "Not Found", "not found");
    return;
  }

  Serial.println("{\"event\":\"log\",\"message\":\"tts/play: request masuk\"}");

  if (strlen(ESP32_TOKEN) > 0 && !token.equals(ESP32_TOKEN)) {
    Serial.println("{\"event\":\"log\",\"message\":\"tts/play: token salah/kosong (401)\"}");
    sendSimple(client, 401, "Unauthorized", "unauthorized");
    return;
  }

  Serial.printf("{\"event\":\"log\",\"message\":\"tts/play: content-length=%u byte\"}\n", (unsigned)contentLength);

  if (contentLength == 0) {
    sendSimple(client, 400, "Bad Request", "audio kosong");
    return;
  }

  ttsPlaying = true;

  streamPcmToI2S(client, contentLength);

  flushI2SSilence();

  ttsPlaying = false;

  Serial.println("{\"event\":\"log\",\"message\":\"tts/play: selesai\"}");

  sendSimple(client, 200, "OK", "ok");
}

void setupHttpServer() {
  ttsServer.begin();
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
// SENSOR CAHAYA (LDR modul 3 pin, output DO) - gelap -> LED menyala
// ==========================================================================

void setupLdr() {
  pinMode(LIGHT_LED_PIN, OUTPUT);
  digitalWrite(LIGHT_LED_PIN, LOW);
  pinMode(LDR_PIN, INPUT);
}

void handleLdr() {
  static unsigned long lastLdrPrintMs = 0;
  static unsigned long changeStartMs = 0;
  static bool pendingDark = false;
  static bool pendingActive = false;

  unsigned long now = millis();
  if (now - lastLdrReadMs < LDR_INTERVAL_MS) {
    return;
  }
  lastLdrReadMs = now;

  int raw = digitalRead(LDR_PIN);
  bool isDark = (raw == LDR_DARK_LEVEL);

  // Debounce: status baru harus bertahan LDR_STABLE_MS sebelum dipakai
  if (isDark != lightLedOn) {
    if (!pendingActive || pendingDark != isDark) {
      pendingActive = true;
      pendingDark = isDark;
      changeStartMs = now;
    } else if (now - changeStartMs >= LDR_STABLE_MS) {
      lightLedOn = isDark;
      digitalWrite(LIGHT_LED_PIN, lightLedOn ? HIGH : LOW);
      pendingActive = false;
    }
  } else {
    pendingActive = false;
  }

  // Print nilai sensor berkala
  if (now - lastLdrPrintMs >= LDR_PRINT_INTERVAL_MS) {
    lastLdrPrintMs = now;

    JsonDocument doc;
    doc["event"] = "light_sensor";
    doc["do_raw"] = raw;
    doc["dark"] = isDark;
    doc["led_on"] = lightLedOn;
    doc["device_id"] = DEVICE_ID;
    serializeJson(doc, Serial);
    Serial.println();
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

    if (ssid.equals(WIFI_SSID)) {
      targetFound = true;
    }
  }

  if (!targetFound) {
    Serial.print("{\"event\":\"log\",\"message\":\"PERINGATAN: SSID '");
    Serial.print(WIFI_SSID);
    Serial.println("' TIDAK ADA di daftar hasil scan!\"}");
  }

  WiFi.scanDelete();
}

void connectWiFi() {

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
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
    currentWifiRetryMs = WIFI_RETRY_MS_MIN;
  } else {
    Serial.print("{\"event\":\"log\",\"message\":\"wifi gagal, status akhir=");
    Serial.print(wifiStatusToText(WiFi.status()));
    Serial.println(", memakai jalur serial saja\"}");
  }

  lastWifiAttemptMs = millis();
}

void ensureWiFi() {

  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  // JANGAN retry WiFi kalau BT sedang proses connect/reconnect - supaya
  // tidak rebutan radio & resource RTOS di saat yang bersamaan.
  if (btConnecting) {
    return;
  }

  if (millis() - lastWifiAttemptMs < currentWifiRetryMs) {
    return;
  }

  lastWifiAttemptMs = millis();

  wsConnected = false;
  paired = false;

  WiFi.disconnect(true);
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  // Backoff: tiap kali gagal, jarak retry berikutnya makin panjang
  // (sampai batas WIFI_RETRY_MS_MAX).
  currentWifiRetryMs = min((unsigned long)WIFI_RETRY_MS_MAX, currentWifiRetryMs * 2);
}


// ==========================================================================
// WEBSOCKET
// ==========================================================================

void registerDevice() {
  JsonDocument doc;
  doc["type"] = "register";
  doc["role"] = "esp32";
  doc["device_id"] = DEVICE_ID;
  doc["ip"] = WiFi.localIP().toString();

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

      if (msgType == "registered") {
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
  setupLdr();
  setupAudioOutput();   // cukup satu kali

  connectWiFi();

  setupHttpServer();

  webSocket.begin(SERVER_HOST, SERVER_PORT, "/ws");
  webSocket.onEvent(onWsEvent);
  webSocket.setReconnectInterval(WS_RECONNECT_MS);
  webSocket.enableHeartbeat(15000, 3000, 2);

  Serial.print("{\"event\":\"log\",\"message\":\"mencoba websocket ke ws://");
  Serial.print(SERVER_HOST);
  Serial.print(":");
  Serial.print(SERVER_PORT);
  Serial.println("/ws\"}");

  playBeep(1000, 90);
  delay(120);
  playBeep(1500, 120);
  delay(150);

  Serial.println("{\"event\":\"log\",\"message\":\"siap: 1=objek 2=ekspresi 3=tulisan + ultrasonik(buzzer) + ldr + tts(push) aktif\"}");
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

  // Terima koneksi HTTP masuk (endpoint /tts/play). Di core 3.x pakai
  // accept(); di core 2.x pakai available().
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  WiFiClient ttsClient = ttsServer.accept();
#else
  WiFiClient ttsClient = ttsServer.available();
#endif
  if (ttsClient) {
    handleTtsClient(ttsClient);
  }

  handleButtons();
  handleUltrasonic();
  handleLdr();
  handleIncomingSerial();
  updateBuzzer();

  setStatusLed(wsConnected);

  static unsigned long lastHeapLog = 0;
  if (millis() - lastHeapLog > 3000) {
    lastHeapLog = millis();
    JsonDocument hb;
    hb["event"] = "log";
    hb["message"] = "heartbeat";
    hb["free_heap"] = ESP.getFreeHeap();
    hb["wifi_rssi"] = WiFi.RSSI();
    hb["wifi_status"] = (int)WiFi.status();
    hb["bt_connected"] = btConnected;
    hb["ws_connected"] = wsConnected;
    serializeJson(hb, Serial);
    Serial.println();
  }

  yield();
  delay(2);
}