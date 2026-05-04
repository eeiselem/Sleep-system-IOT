#include "DHT.h"
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "mbedtls/aes.h"
#include "mbedtls/base64.h"

// =====================
// WiFi + Server Settings
// =====================
const char* ssid = "YOUR_SSID";
const char* password = "YOUR_PASSWORD";

// Replace with your Flask server IP
const char* serverUrl = "YOUR_SERVER_IP";


// =====================
// // AES-128-CBC Configuration
// 16 bytes (128 bits) key and IV. Must match the Python server for decoding.
// =====================
const unsigned char aes_key[16] = "ThisIsKeyAES333";
const unsigned char aes_iv[16]  = "ThisIsVectorIV7";

// =====================
int mq135Baseline = 0;
bool baselineReady = false;
// =====================

// =====================
// Pins
// =====================
#define DHTPIN 4
#define DHTTYPE DHT11

#define MQ135_PIN 34
#define GAS_PIN 36
#define SOUND_PIN 35
#define LIGHT_PIN 32
#define UV_PIN 33

// =====================
// Objects
// =====================
DHT dht(DHTPIN, DHTTYPE);
LiquidCrystal_I2C lcd(0x27, 16, 2);

// =====================
// Timing
// =====================
unsigned long lastPostTime = 0;
const unsigned long postInterval = 10000; // send every 10 seconds

// =====================
// Average analog readings
// =====================
int readAverage(int pin, int samples = 20) {
  long total = 0;

  for (int i = 0; i < samples; i++) {
    total += analogRead(pin);
    delay(5);
  }

  return total / samples;
}

// =====================
// calibrate MQ135
// =====================
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

  mq135Baseline = total / 50;
  baselineReady = true;

  Serial.print("MQ135 baseline: ");
  Serial.println(mq135Baseline);

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Baseline Set:");
  lcd.setCursor(0, 1);
  lcd.print(mq135Baseline);

  delay(2000);
}

// =====================
// Sound peak detector
// =====================
int readSoundPeak(int pin, int samples = 50) {
  int maxValue = 0;

  for (int i = 0; i < samples; i++) {
    int value = analogRead(pin);
    if (value > maxValue) {
      maxValue = value;
    }
    delay(2);
  }

  return maxValue;
}

// =====================
// Room status logic
// =====================
String getRoomStatus(float tempC, float humidity, int mq135, int gas, int sound, int light) {
  if (tempC > 30 || humidity > 70 || mq135 > mq135Baseline + 500 || gas > 2500 || sound > 1200 || light > 2500) {
    return "RED";
  }

  if (tempC > 26 || humidity > 60 || mq135 > mq135Baseline + 200 || gas > 1800 || sound > 700 || light > 1500) {
    return "YELLOW";
  }

  return "GREEN";
}

String getAlertReason(String status, float tempC, float humidity, int mq135, int gas, int sound, int light) {
  if (status == "GREEN") return "Room OK";

  if (tempC > 30) return "Very Hot";
  if (tempC > 26) return "Too Warm";

  if (humidity > 70) return "Very Humid";
  if (humidity > 60) return "Too Humid";

  if (mq135 > 2500 || gas > 2500) return "Bad Air";
  if (mq135 > 2000 || gas > 1800) return "Air Warning";

  if (sound > 1200) return "Very Loud";
  if (sound > 700) return "Too Loud";

  if (light > 2500) return "Very Bright";
  if (light > 1500) return "Too Bright";

  return "Check Room";
}

// =====================
// WiFi setup
// =====================
void connectToWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  Serial.print("Connecting to WiFi");

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Connecting WiFi");

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.print("Connected. ESP32 IP: ");
  Serial.println(WiFi.localIP());

  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("WiFi Connected");
  lcd.setCursor(0, 1);
  lcd.print(WiFi.localIP());

  delay(2000);
}

// =====================
// Send JSON to Flask
// =====================
void sendDataToServer(
  float tempC,
  float humidity,
  int mq135,
  int gas,
  int sound,
  int light,
  int uv,
  float uvVoltage,
  String status,
  String reason
) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected. Reconnecting...");
    WiFi.disconnect();
    WiFi.reconnect();
    return;
  }

  HTTPClient http;
  http.begin(serverUrl);
  http.addHeader("Content-Type", "text/plain");

  StaticJsonDocument<350> doc;

  doc["device_id"] = "environment_node_01";
  doc["temperature"] = tempC;
  doc["humidity"] = humidity;
  doc["mq135"] = mq135;
  doc["gas"] = gas;
  doc["sound"] = sound;
  doc["light"] = light;
  doc["uv"] = uv;
  doc["uv_voltage"] = uvVoltage;
  doc["status"] = status;
  doc["reason"] = reason;

  String payload;
  serializeJson(doc, payload);

  String encryptedPayload = encryptAndEncode(payload);

  Serial.println("Original JSON:");
  Serial.println(payload);

  Serial.println("Encrypted Payload:");
  Serial.println(encryptedPayload);

  int responseCode = http.POST(encryptedPayload);

  Serial.print("HTTP Response Code: ");
  Serial.println(responseCode);

  if (responseCode > 0) {
    Serial.print("Server Response: ");
    Serial.println(http.getString());
  } else {
    Serial.println("POST failed.");
  }

  http.end();
}

void setup() {
  Serial.begin(115200);

  dht.begin();

  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();

  lcd.setCursor(0, 0);
  lcd.print("Sleep Monitor");
  lcd.setCursor(0, 1);
  lcd.print("Starting...");

  delay(2000);

  connectToWiFi();

  calibrateMQ135();

  Serial.println("Environmental node started");
}

void loop() {
  // =====================
  // Read DHT11
  // =====================
  float humidity = dht.readHumidity();
  float tempC = dht.readTemperature();

  if (isnan(humidity) || isnan(tempC)) {
    Serial.println("DHT11 failed");

    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("DHT11 ERROR");
    lcd.setCursor(0, 1);
    lcd.print("Check wiring");

    delay(2000);
    return;
  }

  // =====================
  // Read analog sensors
  // =====================
  int mq135Raw = readAverage(MQ135_PIN);
  int gasRaw = readAverage(GAS_PIN);
  int soundRaw = readSoundPeak(SOUND_PIN);
  int lightRaw = readAverage(LIGHT_PIN);
  int uvRaw = readAverage(UV_PIN);

  float uvVoltage = uvRaw * (3.3 / 4095.0);

  // =====================
  // Status logic
  // =====================
  String status = getRoomStatus(tempC, humidity, mq135Raw, gasRaw, soundRaw, lightRaw);
  String reason = getAlertReason(status, tempC, humidity, mq135Raw, gasRaw, soundRaw, lightRaw);

  // =====================
  // Serial output
  // =====================
  Serial.println("----- Sensor Readings -----");

  Serial.print("Temp: ");
  Serial.print(tempC);
  Serial.print(" C | Humidity: ");
  Serial.print(humidity);
  Serial.println(" %");

  Serial.print("MQ135 Avg: ");
  Serial.println(mq135Raw);

  Serial.print("Gas Avg: ");
  Serial.println(gasRaw);

  Serial.print("Sound Peak: ");
  Serial.println(soundRaw);

  Serial.print("Light Avg: ");
  Serial.println(lightRaw);

  Serial.print("UV Avg: ");
  Serial.print(uvRaw);
  Serial.print(" | Voltage: ");
  Serial.print(uvVoltage);
  Serial.println(" V");

  Serial.print("Room Status: ");
  Serial.println(status);

  Serial.print("Reason: ");
  Serial.println(reason);

  Serial.println();

  // =====================
  // LCD screen 1
  // =====================
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Status:");
  lcd.print(status);

  lcd.setCursor(0, 1);
  lcd.print(reason);

  delay(3000);

  // =====================
  // LCD screen 2
  // =====================
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("T:");
  lcd.print(tempC, 0);
  lcd.print(" H:");
  lcd.print(humidity, 0);
  lcd.print("%");

  lcd.setCursor(0, 1);
  lcd.print("Air:");
  lcd.print(mq135Raw);
  lcd.print(" L:");
  lcd.print(lightRaw);

  delay(3000);

  // =====================
  // Send data every 10 sec
  // =====================
  if (millis() - lastPostTime >= postInterval) {
    sendDataToServer(
      tempC,
      humidity,
      mq135Raw,
      gasRaw,
      soundRaw,
      lightRaw,
      uvRaw,
      uvVoltage,
      status,
      reason
    );

    lastPostTime = millis();
  }
}

String encryptAndEncode(String plainText) {
  mbedtls_aes_context aes;
  mbedtls_aes_init(&aes);
  mbedtls_aes_setkey_enc(&aes, aes_key, 128);

  size_t plainLen = plainText.length();
  size_t paddedLen = plainLen + (16 - (plainLen % 16));

  unsigned char* paddedInput = (unsigned char*)malloc(paddedLen);
  memcpy(paddedInput, plainText.c_str(), plainLen);

  uint8_t padValue = paddedLen - plainLen;
  for (size_t i = plainLen; i < paddedLen; i++) {
    paddedInput[i] = padValue;
  }

  unsigned char* encryptedOutput = (unsigned char*)malloc(paddedLen);

  unsigned char iv_copy[16];
  memcpy(iv_copy, aes_iv, 16);

  mbedtls_aes_crypt_cbc(
    &aes,
    MBEDTLS_AES_ENCRYPT,
    paddedLen,
    iv_copy,
    paddedInput,
    encryptedOutput
  );

  mbedtls_aes_free(&aes);

  size_t base64Len = 0;
  mbedtls_base64_encode(NULL, 0, &base64Len, encryptedOutput, paddedLen);

  unsigned char* base64Output = (unsigned char*)malloc(base64Len + 1);
  mbedtls_base64_encode(base64Output, base64Len, &base64Len, encryptedOutput, paddedLen);
  base64Output[base64Len] = '\0';

  String result = String((char*)base64Output);

  free(paddedInput);
  free(encryptedOutput);
  free(base64Output);

  return result;
}
