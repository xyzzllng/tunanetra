/*
 * Spider Sense - ESP32-CAM (camera endpoint)
 * ===========================================================================
 * ESP32-CAM di sistem ini HANYA memotret. Seluruh AI (YOLO, ekspresi, rambu,
 * Groq, TTS) berjalan di backend Python karena ESP32-CAM tidak mampu
 * menjalankannya (RAM ~520 KB + PSRAM 4 MB).
 *
 * Alur:  tombol ESP32 utama -> backend -> GET http://<ip-cam>/capture -> AI
 *
 * Endpoint:
 *   GET /capture[?flash=1]  -> satu JPEG segar (image/jpeg). flash=1 menyalakan
 *                              LED putih (GPIO4) sesaat selama pengambilan gambar.
 *   GET /status             -> JSON kecil untuk diagnosis (resolusi, RSSI, heap)
 *
 * Autentikasi (opsional, disarankan): kirim header  X-Auth-Token: <CAM_TOKEN>
 * atau query ?token=<CAM_TOKEN>. Kosongkan CAM_TOKEN untuk mematikan.
 *
 * Board : "AI Thinker ESP32-CAM" (PSRAM aktif). Pin di bawah untuk modul AI-Thinker.
 * Core  : arduino-esp32 2.0.x atau 3.x (tanpa library tambahan).
 * Catatan daya: gunakan catu 5 V >= 1 A. Brownout/reset acak hampir selalu karena
 * daya USB lemah.
 * ===========================================================================
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include "secrets.h"

// ============================== KONFIGURASI ===============================

// --- Pin kamera AI-Thinker ESP32-CAM ---
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

#define FLASH_LED_PIN      4     // LED putih di modul
#define FLASH_SETTLE_MS  120     // waktu LED menyala sebelum frame diambil

// Resolusi & kualitas. XGA (1024x768) cukup untuk YOLO 640 px dan rambu; naikkan ke
// FRAMESIZE_SXGA/UXGA bila rambu jauh kurang terbaca (JPEG lebih besar = lebih lambat).
#define CAM_FRAMESIZE        FRAMESIZE_XGA
#define CAM_FRAMESIZE_NAME   "XGA"
#define CAM_JPEG_QUALITY     10  // 0-63, makin kecil makin bagus (dan makin besar)

#define WIFI_RETRY_MS        5000
#define WIFI_LOST_RESTART_MS 90000
#define MAX_CAPTURE_FAILURES 5   // beruntun; lalu restart supaya kamera pulih
#define WRITE_TIMEOUT_MS     8000

// ==========================================================================

WebServer server(80);

static bool havePsram = false;
static int consecutiveFailures = 0;
static unsigned long lastWifiAttemptMs = 0;
static unsigned long wifiLostSinceMs = 0;

// ---------------------------------------------------------------- kamera
bool initCamera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk  = XCLK_GPIO_NUM;
  config.pin_pclk  = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href  = HREF_GPIO_NUM;
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
#else
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
#endif
  config.pin_pwdn  = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  havePsram = psramFound();
  if (havePsram) {
    config.frame_size   = CAM_FRAMESIZE;
    config.jpeg_quality = CAM_JPEG_QUALITY;
    config.fb_count     = 2;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;   // selalu frame terbaru, bukan yang antre
  } else {
    // Tanpa PSRAM: resolusi kecil, hanya untuk uji sambungan.
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = 12;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("{\"event\":\"log\",\"message\":\"kamera gagal init: 0x%x\"}\n", err);
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  if (s != nullptr) {
    s->set_vflip(s, 0);
    s->set_hmirror(s, 0);
  }
  return true;
}

// ------------------------------------------------------------------ HTTP
bool authOk() {
  if (strlen(CAM_TOKEN) == 0) {
    return true;
  }
  if (server.header("X-Auth-Token").equals(CAM_TOKEN)) {
    return true;
  }
  return server.hasArg("token") && server.arg("token").equals(CAM_TOKEN);
}

// Buang frame yang sudah lama menunggu di buffer supaya hasil benar-benar terkini.
void discardFrames(int count) {
  for (int i = 0; i < count; i++) {
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale != nullptr) {
      esp_camera_fb_return(stale);
    }
  }
}

void handleCapture() {
  if (!authOk()) {
    server.send(401, "text/plain", "unauthorized");
    return;
  }

  bool flash = server.hasArg("flash") && server.arg("flash") == "1";
  if (flash) {
    digitalWrite(FLASH_LED_PIN, HIGH);
    delay(FLASH_SETTLE_MS);
  }

  discardFrames(flash ? 3 : 1);          // dengan flash: beri waktu auto-exposure menyesuaikan
  camera_fb_t *fb = esp_camera_fb_get();

  if (flash) {
    digitalWrite(FLASH_LED_PIN, LOW);
  }

  if (fb == nullptr) {
    consecutiveFailures++;
    Serial.printf("{\"event\":\"log\",\"message\":\"capture gagal (%d beruntun)\"}\n", consecutiveFailures);
    server.send(503, "text/plain", "camera capture failed");
    if (consecutiveFailures >= MAX_CAPTURE_FAILURES) {
      delay(200);
      ESP.restart();
    }
    return;
  }
  consecutiveFailures = 0;

  WiFiClient client = server.client();
  server.setContentLength(fb->len);
  server.sendHeader("Cache-Control", "no-store");
  server.send(200, "image/jpeg", "");    // hanya header; badan ditulis manual di bawah

  size_t sent = 0;
  unsigned long lastProgress = millis();
  while (sent < fb->len && client.connected()) {
    size_t chunk = fb->len - sent;
    if (chunk > 4096) {
      chunk = 4096;
    }
    size_t written = client.write(fb->buf + sent, chunk);
    if (written == 0) {
      if (millis() - lastProgress > WRITE_TIMEOUT_MS) {
        break;
      }
      delay(2);
      continue;
    }
    sent += written;
    lastProgress = millis();
  }

  esp_camera_fb_return(fb);
}

void handleStatus() {
  if (!authOk()) {
    server.send(401, "text/plain", "unauthorized");
    return;
  }
  String json = "{";
  json += "\"device\":\"esp32cam\"";
  json += ",\"framesize\":\"" CAM_FRAMESIZE_NAME "\"";
  json += ",\"psram\":" + String(havePsram ? "true" : "false");
  json += ",\"rssi\":" + String(WiFi.RSSI());
  json += ",\"heap\":" + String(ESP.getFreeHeap());
  json += ",\"ip\":\"" + WiFi.localIP().toString() + "\"";
  json += ",\"uptime_s\":" + String(millis() / 1000);
  json += ",\"capture_failures\":" + String(consecutiveFailures);
  json += "}";
  server.send(200, "application/json", json);
}

// ----------------------------------------------------------------- Wi-Fi
void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                 // latensi rendah; penting untuk capture cepat
  WiFi.setHostname(CAM_HOSTNAME);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
  }
  lastWifiAttemptMs = millis();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("{\"event\":\"log\",\"message\":\"wifi ok, ip=");
    Serial.print(WiFi.localIP());
    Serial.println("\"}");
    if (MDNS.begin(CAM_HOSTNAME)) {
      MDNS.addService("http", "tcp", 80);
    }
  } else {
    Serial.println("{\"event\":\"log\",\"message\":\"wifi gagal; akan dicoba lagi\"}");
  }
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    wifiLostSinceMs = 0;
    return;
  }
  if (wifiLostSinceMs == 0) {
    wifiLostSinceMs = millis();
  }
  if (millis() - wifiLostSinceMs > WIFI_LOST_RESTART_MS) {
    ESP.restart();                       // jaringan hilang lama: mulai bersih
  }
  if (millis() - lastWifiAttemptMs < WIFI_RETRY_MS) {
    return;
  }
  lastWifiAttemptMs = millis();
  WiFi.disconnect(true);
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// ------------------------------------------------------------------ setup
void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

  if (!initCamera()) {
    Serial.println("{\"event\":\"log\",\"message\":\"kamera tidak siap; restart dalam 5 detik\"}");
    delay(5000);
    ESP.restart();
  }

  connectWiFi();

  const char *headerKeys[] = { "X-Auth-Token" };
  server.collectHeaders(headerKeys, 1);
  server.on("/capture", HTTP_GET, handleCapture);
  server.on("/status", HTTP_GET, handleStatus);
  server.onNotFound([]() { server.send(404, "text/plain", "not found"); });
  server.begin();

  Serial.printf("{\"event\":\"log\",\"message\":\"esp32-cam siap; psram=%d heap=%u\"}\n",
                havePsram ? 1 : 0, (unsigned)ESP.getFreeHeap());
}

void loop() {
  ensureWiFi();
  server.handleClient();
  delay(2);
}
