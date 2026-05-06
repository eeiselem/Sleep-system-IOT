/**
 * Environmental node sketch for the sleep dashboard.
 *
 * Main job:
 * - Read room sensors (temp, humidity, air proxy, sound, light, UV).
 * - Optionally include gyro variance when MPU6050 is connected.
 * - Encrypt each field with AES-256-GCM and POST to /post-environment.
 *
 * Config you must align with the Flask server:
 * - MASTER_ENC_SECRET_UTF8 must match MASTER_ENCRYPTION_KEY in .env.
 * - INGEST_API_KEY_STR must match INGEST_API_KEY in .env.
 * - Wi-Fi comes from ESP32/secrets.h via #include "../secrets.h".
 *
 * Analog stability notes:
 * - ESP32 ADC can occasionally drop to zero after Wi-Fi or bus activity.
 * - This sketch discards first reads, uses median filtering, and smooths outputs.
 * - That keeps noise/light values stable for dashboard demos.
 *
 * Dashboard scale mapping:
 * - air_quality is normalized to 0..500 to match UI scoring.
 * - ambient_noise and ambient_light are mapped to pseudo-dB / pseudo-lux ranges.
 */
#include "../secrets.h"

#include "DHT.h"
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <base64.h>
#include <cstring>
#include "esp_random.h"
#include "mbedtls/gcm.h"
#include "mbedtls/sha256.h"
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <vector>
#include <cmath>

// --- Wi-Fi / server (must match Flask .env) ---
wifi_mode_t wifi_mode = WIFI_STA;
// Base URL only — no path (e.g. http://192.168.1.10:8888/ or https://xxx.trycloudflare.com/)
const String server_url = "http://192.168.1.1:8888/";
static const char *MASTER_ENC_SECRET_UTF8 = "0123456789123456";
static const char *INGEST_API_KEY_STR = "ingest-api-key";

// --- Pins ---
#define DHTPIN 4
#define DHTTYPE DHT11
#define MQ135_PIN 34
#define GAS_PIN 36
#define SOUND_PIN 35
#define LIGHT_PIN 32
#define UV_PIN 33
constexpr uint8_t I2C_SDA_PIN = 21;
constexpr uint8_t I2C_SCL_PIN = 22;

// --- Objects ---
DHT dht(DHTPIN, DHTTYPE);
LiquidCrystal_I2C lcd(0x27, 16, 2);
Adafruit_MPU6050 mpu;
bool mpu_ready = false;

int mq135Baseline = 0;

float temperature = 0;
float humidity = 0;
float air_quality_level = 0;
float ambient_noise = 0;
float light_level = 0;
float gyro_variance = 0;
std::vector<float> gyro_mag_window;
const size_t GYRO_WINDOW_SIZE = 20;

struct Stats {
  float mean;
  float stdDev;
};

unsigned long last_wifi_retry_time = 0;
unsigned long last_post_time = 0;
unsigned long last_dht_time = 0;
unsigned long last_analog_time = 0;
unsigned long last_mpu_time = 0;
unsigned long last_lcd_time = 0;

const unsigned long READING_TIME_INTERVAL_MS = 2000;
// Analog burst can take >250 ms; interval must exceed worst-case read time or samples collapse to 0 intermittently.
const unsigned long ANALOG_SAMPLE_INTERVAL_MS = 600;
const unsigned long MPU_SAMPLE_INTERVAL_MS = 40;
const unsigned long UPLOAD_INTERVAL_MS = 10000;
const unsigned long LCD_ROTATE_MS = 3000;

// EMA on mapped noise/lux to suppress single bad ADC samples (0 glitches).
const float ANALOG_SMOOTH_ALPHA = 0.45f;

static int median3(int a, int b, int c) {
  if (a > b) {
    int t = a;
    a = b;
    b = t;
  }
  if (b > c) {
    int t = b;
    b = c;
    c = t;
  }
  if (a > b) {
    int t = a;
    a = b;
    b = t;
  }
  return b;
}

// First read after WiFi / channel change is often garbage on ESP32.
static void analogDiscardRead(int pin) {
  (void)analogRead(pin);
  delayMicroseconds(120);
}

int readAverage(int pin, int samples = 20) {
  long total = 0;
  analogDiscardRead(pin);
  for (int i = 0; i < samples; i++) {
    total += analogRead(pin);
    delay(4);
  }
  return (int)(total / samples);
}

int readSoundPeak(int pin, int samples = 50) {
  int maxValue = 0;
  analogDiscardRead(pin);
  for (int i = 0; i < samples; i++) {
    int value = analogRead(pin);
    if (value > maxValue) maxValue = value;
    delay(2);
  }
  return maxValue;
}

// Median of three single ADC reads (fast; kills one-off zero glitches).
static int analogMedianQuick(int pin) {
  analogDiscardRead(pin);
  delayMicroseconds(90);
  int a = analogRead(pin);
  delayMicroseconds(140);
  int b = analogRead(pin);
  delayMicroseconds(140);
  int c = analogRead(pin);
  return median3(a, b, c);
}

// Median of three short peak bursts — rejects a burst that randomly reads all zeros.
static int soundPeakMedianBursts(int pin) {
  int bursts[3];
  for (int bi = 0; bi < 3; bi++) {
    analogDiscardRead(pin);
    int mx = 0;
    for (int i = 0; i < 28; i++) {
      int v = analogRead(pin);
      if (v > mx) mx = v;
      delayMicroseconds(240);
    }
    bursts[bi] = mx;
    delayMicroseconds(400);
  }
  return median3(bursts[0], bursts[1], bursts[2]);
}

void calibrateMQ135() {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Calibrating Air");
  lcd.setCursor(0, 1);
  lcd.print("Please wait...");
  long total = 0;
  for (int i = 0; i < 50; i++) {
    total += readAverage(MQ135_PIN);
    delay(100);
  }
  mq135Baseline = (int)(total / 50);
  Serial.print("MQ135 baseline: ");
  Serial.println(mq135Baseline);
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Baseline Set:");
  lcd.setCursor(0, 1);
  lcd.print(mq135Baseline);
  delay(2000);
}

String getRoomStatus(float tempC, float hum, int mq135, int gas, int sound, int light) {
  if (tempC > 30 || hum > 70 || mq135 > mq135Baseline + 500 || gas > 2500 || sound > 1200 || light > 2500) {
    return "RED";
  }
  if (tempC > 26 || hum > 60 || mq135 > mq135Baseline + 200 || gas > 1800 || sound > 700 || light > 1500) {
    return "YELLOW";
  }
  return "GREEN";
}

String getAlertReason(const String &status, float tempC, float hum, int mq135, int gas, int sound, int light) {
  if (status == "GREEN") return "Room OK";
  if (tempC > 30) return "Very Hot";
  if (tempC > 26) return "Too Warm";
  if (hum > 70) return "Very Humid";
  if (hum > 60) return "Too Humid";
  if (mq135 > 2500 || gas > 2500) return "Bad Air";
  if (mq135 > 2000 || gas > 1800) return "Air Warning";
  if (sound > 1200) return "Very Loud";
  if (sound > 700) return "Too Loud";
  if (light > 2500) return "Very Bright";
  if (light > 1500) return "Too Bright";
  return "Check Room";
}

void initWiFi() {
  WiFi.mode(wifi_mode);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  last_wifi_retry_time = millis();
  Serial.println("WiFi connect started.");
}

void handle_wifi_reconnect(unsigned long current_time) {
  static bool wifi_was_connected = false;
  if (WiFi.status() != WL_CONNECTED) {
    wifi_was_connected = false;
    if (current_time - last_wifi_retry_time >= 5000) {
      Serial.println("WiFi lost. Reconnecting...");
      WiFi.disconnect();
      WiFi.reconnect();
      last_wifi_retry_time = current_time;
    }
    return;
  }
  if (!wifi_was_connected) {
    Serial.print("WiFi OK IP ");
    Serial.println(WiFi.localIP());
    wifi_was_connected = true;
  }
}

static void sha256_secret_to_aes256_key(unsigned char out32[32]) {
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  mbedtls_sha256_update(
      &ctx,
      reinterpret_cast<const unsigned char *>(MASTER_ENC_SECRET_UTF8),
      strlen(MASTER_ENC_SECRET_UTF8));
  mbedtls_sha256_finish(&ctx, out32);
  mbedtls_sha256_free(&ctx);
}

String encrypt_transport_gcm(const String &plainUtf8) {
  const size_t n = plainUtf8.length();
  if (n == 0 || n > 220) return String("");

  unsigned char aesKey[32];
  sha256_secret_to_aes256_key(aesKey);
  unsigned char nonce[12];
  esp_fill_random(nonce, sizeof(nonce));
  unsigned char tag[16];
  unsigned char ct[224];
  mbedtls_gcm_context gcm;
  mbedtls_gcm_init(&gcm);
  String out;
  do {
    if (mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, aesKey, 256) != 0) break;
    memset(ct, 0, sizeof(ct));
    if (mbedtls_gcm_crypt_and_tag(
            &gcm,
            MBEDTLS_GCM_ENCRYPT,
            n,
            nonce,
            sizeof(nonce),
            nullptr,
            0,
            reinterpret_cast<const unsigned char *>(plainUtf8.c_str()),
            ct,
            sizeof(tag),
            tag) != 0) {
      break;
    }
    constexpr size_t kBlobCap = 12 + 16 + 224;
    unsigned char blob[kBlobCap];
    memcpy(blob, nonce, 12);
    memcpy(blob + 12, tag, 16);
    memcpy(blob + 28, ct, n);
    out = base64::encode(blob, 28 + n);
  } while (0);
  mbedtls_gcm_free(&gcm);
  return out;
}

Stats calculateStats(std::vector<float> &data) {
  Stats result = {0, 0};
  if (data.empty()) return result;
  float sum = 0;
  for (float v : data) sum += v;
  result.mean = sum / data.size();
  float sumSqDev = 0;
  for (float v : data) sumSqDev += (v - result.mean) * (v - result.mean);
  float variance = sumSqDev / data.size();
  result.stdDev = sqrt(variance);
  return result;
}

static float scale_air_quality_for_ui(float adc_blend) {
  float v = adc_blend;
  if (v < 0.f) v = 0.f;
  if (v > 4095.f) v = 4095.f;
  return min(500.f, (v / 4095.f) * 500.f);
}

static float adc_peak_to_display_db(float peak_adc) {
  float p = peak_adc;
  if (p < 0.f) p = 0.f;
  if (p > 4095.f) p = 4095.f;
  return 28.f + (p / 4095.f) * 42.f;
}

static float adc_avg_to_display_lux(float avg_adc) {
  float a = avg_adc;
  if (a < 0.f) a = 0.f;
  if (a > 4095.f) a = 4095.f;
  return (a / 4095.f) * 800.f;
}

void post_environment() {
  Serial.println("POST /post-environment ...");

  HTTPClient http;
  WiFiClientSecure tlsClient;
  WiFiClient plainClient;

  String full_server_url = server_url + "/post-environment";
  String su = server_url;
  su.trim();
  bool use_https = false;
  if (su.length() >= 8) {
    String head = su.substring(0, 8);
    head.toLowerCase();
    use_https = (head == "https://");
  }

  bool ok = false;
  if (use_https) {
    tlsClient.setInsecure();
    ok = http.begin(tlsClient, full_server_url);
  } else {
    ok = http.begin(plainClient, full_server_url);
  }
  if (!ok) {
    Serial.println("http.begin failed.");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-KEY", INGEST_API_KEY_STR);

  StaticJsonDocument<2048> doc;
  doc["temperature"] = encrypt_transport_gcm(String(temperature, 2));
  doc["humidity"] = encrypt_transport_gcm(String(humidity, 2));
  doc["air_quality"] = encrypt_transport_gcm(String(air_quality_level, 2));
  doc["ambient_noise"] = encrypt_transport_gcm(String(ambient_noise, 2));
  doc["ambient_light"] = encrypt_transport_gcm(String(light_level, 2));
  if (mpu_ready && gyro_mag_window.size() > 1) {
    doc["gyro_variance"] = encrypt_transport_gcm(String(gyro_variance, 4));
  }

  String payload;
  serializeJson(doc, payload);

  int code = http.POST(payload);
  if (code > 0) {
    Serial.print("Response ");
    Serial.print(code);
    Serial.print(": ");
    Serial.println(http.getString());
  } else {
    Serial.println("No HTTP response.");
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(10);
#if defined(ESP32)
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);
#endif

  dht.begin();
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("Env + Server");
  lcd.setCursor(0, 1);
  lcd.print("Starting...");

  if (mpu.begin()) {
    mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    mpu_ready = true;
    Serial.println("MPU6050 OK");
  } else {
    Serial.println("MPU6050 not found (gyro_variance omitted).");
  }

  delay(800);
  calibrateMQ135();

  initWiFi();
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();

#if defined(ESP32)
  WiFi.setSleep(false);
#endif

  Serial.println("Environmental_Device ready (POST /post-environment).");
}

void loop() {
  unsigned long now = millis();
  handle_wifi_reconnect(now);

  if (now - last_analog_time >= ANALOG_SAMPLE_INTERVAL_MS) {
    last_analog_time = now;
    yield();

    int mq135Raw = readAverage(MQ135_PIN, 14);
    int gasRaw = readAverage(GAS_PIN, 14);
    int peakSound = soundPeakMedianBursts(SOUND_PIN);
    int avgLight = analogMedianQuick(LIGHT_PIN);

    static int last_peak_raw = 0;
    static int last_light_raw = 0;
    const int GLITCH_SOUND = 72;
    const int GLITCH_LIGHT = 64;
    if (peakSound == 0 && last_peak_raw >= GLITCH_SOUND) {
      peakSound = max(1, last_peak_raw / 6);
    }
    if (avgLight == 0 && last_light_raw >= GLITCH_LIGHT) {
      avgLight = max(1, last_light_raw / 6);
    }
    last_peak_raw = peakSound;
    last_light_raw = avgLight;

    float blend = 0.7f * static_cast<float>(mq135Raw) + 0.3f * static_cast<float>(gasRaw);
    air_quality_level = scale_air_quality_for_ui(blend);

    float n_db = adc_peak_to_display_db(static_cast<float>(peakSound));
    float lx = adc_avg_to_display_lux(static_cast<float>(avgLight));

    static bool analog_smooth_init = false;
    if (!analog_smooth_init) {
      ambient_noise = n_db;
      light_level = lx;
      analog_smooth_init = true;
    } else {
      ambient_noise =
          ambient_noise * (1.f - ANALOG_SMOOTH_ALPHA) + n_db * ANALOG_SMOOTH_ALPHA;
      light_level =
          light_level * (1.f - ANALOG_SMOOTH_ALPHA) + lx * ANALOG_SMOOTH_ALPHA;
    }

    yield();
  }

  if (mpu_ready && (now - last_mpu_time >= MPU_SAMPLE_INTERVAL_MS)) {
    last_mpu_time = now;
    sensors_event_t a, g, temp_event;
    mpu.getEvent(&a, &g, &temp_event);
    float gm = sqrt(g.gyro.x * g.gyro.x + g.gyro.y * g.gyro.y + g.gyro.z * g.gyro.z);
    gyro_mag_window.push_back(gm);
    if (gyro_mag_window.size() > GYRO_WINDOW_SIZE) {
      gyro_mag_window.erase(gyro_mag_window.begin());
    }
    if (gyro_mag_window.size() > 1) {
      Stats st = calculateStats(gyro_mag_window);
      gyro_variance = st.stdDev * st.stdDev;
    }
  }

  if (now - last_dht_time >= READING_TIME_INTERVAL_MS) {
    last_dht_time = now;
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t) && !isnan(h)) {
      temperature = t;
      humidity = h;
    }
  }

  if (now - last_lcd_time >= LCD_ROTATE_MS) {
    last_lcd_time = now;
    int mq135Snap = readAverage(MQ135_PIN, 8);
    int gasSnap = readAverage(GAS_PIN, 8);
    int soundSnap = readSoundPeak(SOUND_PIN, 24);
    int lightSnap = readAverage(LIGHT_PIN, 8);
    int uvSnap = readAverage(UV_PIN, 8);
    float uvV = uvSnap * (3.3f / 4095.0f);

    String status = getRoomStatus(temperature, humidity, mq135Snap, gasSnap, soundSnap, lightSnap);
    String reason = getAlertReason(status, temperature, humidity, mq135Snap, gasSnap, soundSnap, lightSnap);

    static uint8_t lcd_page = 0;
    lcd_page = (uint8_t)((lcd_page + 1) % 3);
    lcd.clear();
    if (lcd_page == 0) {
      lcd.setCursor(0, 0);
      lcd.print("St:");
      lcd.print(status);
      lcd.setCursor(0, 1);
      String r = reason;
      if (r.length() > 16) r = r.substring(0, 16);
      lcd.print(r);
    } else if (lcd_page == 1) {
      lcd.setCursor(0, 0);
      lcd.print("T:");
      lcd.print(temperature, 0);
      lcd.print(" H:");
      lcd.print(humidity, 0);
      lcd.print("%");
      lcd.setCursor(0, 1);
      lcd.print("MQ:");
      lcd.print(mq135Snap);
      lcd.print(" L:");
      lcd.print(lightSnap);
    } else {
      lcd.setCursor(0, 0);
      lcd.print("UVadc:");
      lcd.print(uvSnap);
      lcd.setCursor(0, 1);
      lcd.print("Uv:");
      lcd.print(uvV, 2);
      lcd.print("V");
    }
  }

  if (WiFi.status() == WL_CONNECTED && (now - last_post_time >= UPLOAD_INTERVAL_MS)) {
    last_post_time = now;
    post_environment();
  }
}
