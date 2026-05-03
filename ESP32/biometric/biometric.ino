#include <Wire.h>             // i2c
#include <Adafruit_MPU6050.h> // gyro
#include <Adafruit_Sensor.h>  // gyro
#include "MAX30105.h"         // hr spo2 sensor lib
#include "spo2_algorithm.h"   // calc hr and spo2
#include <WiFi.h>             // WiFi connection
#include <HTTPClient.h>       // HTTP send / recieve
#include <ArduinoJson.h>      // JSON
#include "mbedtls/aes.h"      // encryption
#include "mbedtls/base64.h"   // Base64

// Wi-Fi credentials
const String ssid = "YOUR_SSID";           // WiFi SSID
const String password = "YOUR_PASSWORD";  // WiFi password

// Wi-Fi mode
wifi_mode_t wifi_mode = WIFI_STA;  // WiFi connection type

// Server config
const String server_url = "YOUR_SERVER_IP";  // Server url; update from running server file output

// AES-128-CBC Configuration
// 16 bytes (128 bits) key and IV. Must match the Python server for decoding.
const unsigned char aes_key[16] = "ThisIsKeyAES333";
const unsigned char aes_iv[16] = "ThisIsVectorIV7";

MAX30105 particleSensor;

Adafruit_MPU6050 mpu;

// --- Sleep Tracking Constants ---
const unsigned long GYRO_REPORT_WINDOW = 30 * 1000;
const unsigned long GYRO_SAMPLE_INTERVAL = 100; // 10Hz sampling (100ms)
const float GYRO_NOISE_FLOOR = 0.8;
const float GYRO_SENSITIVITY = 100.0;

// Variables
unsigned long gyro_lastSampleTime = 0;
unsigned long gyro_startTime = 0;
float gyro_movementEnergy = 0;
float last_gyro_sleepRating = 0;

#define BUFFER_SIZE 100  // size of sliding window buffer

// buffer for each light
uint32_t irBuffer[BUFFER_SIZE];
uint32_t redBuffer[BUFFER_SIZE];

uint16_t bufferIndex = 0;
bool bufferFull = false;

int32_t spo2; // current spo2 val
int8_t validSPO2;
int32_t lastValidSpo2; // last valid spo2
int32_t heartRate; // current hr val
int8_t validHeartRate;
int32_t lastValidHeartRate; // last valid hr

// HRV Global Variables
float hrv_rmssd = 0; // Current HRV score (Root Mean Square of Successive Differences) in ms
uint32_t lastBeatTime = 0; // Millis timestamp of the last detected pulse peak
float sumSqDiff = 0; // Cumulative sum of squared differences between consecutive beats
int beatCount = 0; // Total number of valid beats used in the current HRV calculation
uint32_t lastRR = 0; // The time interval (ms) between the two most recent beats

bool readyToCompute = false;
int stableCount = 0;

unsigned long current_time = 0;
unsigned long last_post_time = 0;
unsigned long last_wifi_retry_time = 0;
int upload_interval = 30 * 1000; // 30 seconds

void setup() {
  Serial.begin(115200);  // start serial

  // Reduces the serial timeout to 10ms. This prevents readStringUntil() from blocking the main sensor loop
  Serial.setTimeout(10);

  initWiFi();  // Init wifi connection

  Wire.begin(21, 22);  // init i2c

  setupGyro(); // setup gyro sensor

  setupHrSpo2(); // setup the hr spo2 sensor
}

// handle setup of gyro sensor
void setupGyro() {
  if (!mpu.begin()) { while (1); }

  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ); 
  
  unsigned long now = millis();
  gyro_startTime = now;
  gyro_lastSampleTime = now;
}

// handle setup of hr and spo2 sensor
void setupHrSpo2() {
  // check for sensor
  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("Sensor not found!");
    while (1)
      ;
  }

  // sensor config
  byte ledBrightness = 60;  // 0 to 255 LED brightness
  byte sampleAverage = 4;
  byte ledMode = 2;      // use red and ir LEDs
  int sampleRate = 25;  // samples per second
  int pulseWidth = 411;
  int adcRange = 4096;

  // clear fifo
  particleSensor.clearFIFO();
  delay(100);

  // setup sensor
  particleSensor.setup(ledBrightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange);

  Serial.println("Stabilizing... please wait.");

  // pre fill buffer
  for (int i = 0; i < BUFFER_SIZE; i++) {
    unsigned long start = millis();

    while (!particleSensor.available()) {
      particleSensor.check();

      if (millis() - start > 1000) {
        Serial.println("Sensor timeout during stabilizing");
        return;
      }
    }

    irBuffer[i] = particleSensor.getIR();
    redBuffer[i] = particleSensor.getRed();
    particleSensor.nextSample();

    if (i % 10 == 0) Serial.print(".");
  }
  Serial.println("\nRunning!");
}

void loop() {
  current_time = millis();  // set current time

  handle_wifi_reconnect(current_time);  // reconnect WiFi if needed

  gyro(); // update gyro readings

  hrSpo2(); // update hr and spo2 reading

  // send data to server
  if (current_time - last_post_time >= upload_interval) {
    // Just verify we actually have WiFi before trying to POST
    if (WiFi.status() == WL_CONNECTED) {
      post_data();
    }
    last_post_time = current_time;  // Reset stopwatch
  }
}

// handle collecting gyro data
void gyro() {
  // collect data at interval
  if (current_time - gyro_lastSampleTime >= GYRO_SAMPLE_INTERVAL) {
    gyro_lastSampleTime = current_time;

    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);

    float gyro_mag = sqrt(sq(a.acceleration.x) + sq(a.acceleration.y) + sq(a.acceleration.z));
    float gyro_delta = abs(gyro_mag - 9.81);

    if (gyro_delta > GYRO_NOISE_FLOOR) {
      gyro_movementEnergy += gyro_delta;
    }
  }

  // report resuts at interval
  if (current_time - gyro_startTime >= GYRO_REPORT_WINDOW) {
    float gyro_sleepRating = (gyro_movementEnergy / GYRO_SENSITIVITY) * 100.0; 
    
    if (gyro_sleepRating > 100) gyro_sleepRating = 100;

    Serial.print("Activity Rating: ");
    Serial.print(gyro_sleepRating, 2);
    Serial.println("%");
    last_gyro_sleepRating = gyro_sleepRating;

    // Reset for next window
    gyro_movementEnergy = 0;
    gyro_startTime = current_time;
  }
}

// handle collecting hr and spo2 data
void hrSpo2() {
  while (!particleSensor.available()) particleSensor.check();

  uint32_t red = particleSensor.getRed();
  uint32_t ir = particleSensor.getIR();
  particleSensor.nextSample();

  static bool fingerPresent = false;
  static int stableCount = 0;
  static bool bufferReady = false;

  if (ir < 30000) {
    if (fingerPresent) {
      Serial.println("FINGER REMOVED");
    }
    fingerPresent = false;
    stableCount = 0;
    bufferReady = false;
    return;
  }

  if (!fingerPresent) {
    Serial.println("FINGER DETECTED");
    stableCount = 0;
    bufferReady = false;

    // reset buffer
    for (int i = 0; i < BUFFER_SIZE; i++) {
      irBuffer[i] = ir;
      redBuffer[i] = red;
    }
  }

  fingerPresent = true;

  // hr rr slide buffer window
  for (int i = 1; i < BUFFER_SIZE; i++) {
    redBuffer[i - 1] = redBuffer[i];
    irBuffer[i - 1] = irBuffer[i];
  }

  redBuffer[BUFFER_SIZE - 1] = red;
  irBuffer[BUFFER_SIZE - 1] = ir;

  stableCount++;

  if (stableCount < BUFFER_SIZE) {
    Serial.println("Stabilizing signal...");
    return;
  }

  bufferReady = true;

  if (!bufferReady) return;

  static unsigned long lastCalc = 0;
  if (millis() - lastCalc > 1000) {
    lastCalc = millis();

    // hrv vars
    uint32_t bufferLastBeatIndex = 0;
    float currentSumSqDiff = 0;
    int currentBeatCount = 0;
    uint32_t prevRR = 0;

    // Scan the buffer for peaks
    for (int i = 1; i < BUFFER_SIZE; i++) {
      uint32_t currentVal = irBuffer[i];
      uint32_t prevVal = irBuffer[i - 1];
      uint32_t dynamicThresh = currentVal / 500;

      // Detect the drop after a peak
      if (prevVal > currentVal && (prevVal - currentVal) > dynamicThresh) {
        // Calculate RR based on the index difference multiplied by sample time
        uint32_t sampleTimeMs = 10; 
        uint32_t rrInterval = (i - bufferLastBeatIndex) * sampleTimeMs;

        // Validate the interval
        if (rrInterval >= 450 && rrInterval < 1200) {
          if (prevRR > 0) {
            float diff = abs((float)rrInterval - (float)prevRR);
            if (diff < 200) { // Physiological filter
              currentSumSqDiff += (diff * diff);
              currentBeatCount++;
            }
          }
          prevRR = rrInterval;
          bufferLastBeatIndex = i;
        }
      }
    }

    // Update global HRV if we found enough beats in this window
    if (currentBeatCount > 0) {
      hrv_rmssd = sqrt(currentSumSqDiff / currentBeatCount);
    }
  }

  uint32_t irMin = irBuffer[0];
  uint32_t irMax = irBuffer[0];

  for (int i = 1; i < BUFFER_SIZE; i++) {
    if (irBuffer[i] < irMin) irMin = irBuffer[i];
    if (irBuffer[i] > irMax) irMax = irBuffer[i];
  }

  if ((irMax - irMin) < 1000) {
    Serial.println("Weak signal");
    return;
  }
  
  // calc hr and spo2
  maxim_heart_rate_and_oxygen_saturation(
    irBuffer,
    BUFFER_SIZE,
    redBuffer,
    &spo2,
    &validSPO2,
    &heartRate,
    &validHeartRate);

  // report if valid data
  if (validHeartRate == 1 && heartRate > 30 && heartRate < 220) {
    lastValidHeartRate = heartRate;

    Serial.print("BPM: ");
    Serial.print(heartRate);
    Serial.print(" | HRV (RMSSD): "); Serial.print(hrv_rmssd); 
    Serial.print(" ms");

    if (validSPO2 == 1 && spo2 > 70 && spo2 <= 100) {
      lastValidSpo2 = spo2;
      Serial.print(" | SpO2: ");
      Serial.print(spo2);
      Serial.println("%");
    } else {
      Serial.println(" | SpO2: --");
    }

  } else {
    Serial.println("Reading...");
  }
}

// Setup WiFi conection
void initWiFi() {
  // Setup connection
  WiFi.mode(wifi_mode);
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi...");

  // Wait for connection
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print('.');
    delay(1000);
  }

  // Connected, print update
  Serial.print("\nConnected to SSID: ");
  Serial.print(WiFi.SSID());
  Serial.print(" Local IP: ");
  Serial.println(WiFi.localIP());
}

// Handles WiFi reconnect if disconnected
// Call before doing any WiFi data transfer
void handle_wifi_reconnect(unsigned long current_time) {
  if (WiFi.status() != WL_CONNECTED) {
    // Only try to reconnect every 5 seconds so we don't spam the router
    if (current_time - last_wifi_retry_time >= 5000) {
      Serial.println("WiFi connection lost. Attempting to reconnect...");
      WiFi.disconnect();
      WiFi.reconnect();
      last_wifi_retry_time = current_time;
    }
  }
}

// Posts data to http route
void post_data() {
  if (lastValidHeartRate > 0 && lastValidSpo2 > 0) {
    Serial.println("Attempting to send data...");

    HTTPClient http;                                     // Init http
    String full_server_url = server_url + "/biometric";  // Full route url
    http.begin(full_server_url);                         // Begin http send
    http.addHeader("Content-Type", "application/json");  // http header

    // Create json document
    StaticJsonDocument<200> data_doc;
    data_doc["heart_rate"] = String(lastValidHeartRate);
    data_doc["hrv"] = String(hrv_rmssd);
    data_doc["spo2"] = String(lastValidSpo2);
    data_doc["gyro"] = String(last_gyro_sleepRating);
    String payload;                    // will hold payload as string
    serializeJson(data_doc, payload);  // Save json payload as string
    String encryptedPayload = encryptAndEncode(payload);

    // Show data encryptioon process
    Serial.println("==============================================================================================================");
    Serial.print("Payload: ");
    Serial.println(payload);
    Serial.println(encryptedPayload);
    Serial.println("==============================================================================================================");

    int http_response_code = http.POST(encryptedPayload);  // Send data and save response

    // If response, show it
    if (http_response_code > 0) {
      Serial.print("Response: ");
      Serial.println(http.getString());
    } else {  // No respoonse was received
      Serial.println("No response received from server.");
    }

    http.end();  // Close http
    Serial.println("Data transmission complete.");
  }
}

// Apply PKCS#7 Padding & Encrypt JSON via AES-128-CBC, then Base64 Encode
String encryptAndEncode(String plainText) {
  mbedtls_aes_context aes;
  mbedtls_aes_init(&aes);
  mbedtls_aes_setkey_enc(&aes, aes_key, 128);

  // Calculate padded length (PKCS#7)
  size_t plainLen = plainText.length();
  size_t paddedLen = plainLen + (16 - (plainLen % 16));
  unsigned char* paddedInput = (unsigned char*)malloc(paddedLen);
  memcpy(paddedInput, plainText.c_str(), plainLen);

  // Apply padding
  uint8_t padValue = paddedLen - plainLen;
  for (size_t i = plainLen; i < paddedLen; i++) {
    paddedInput[i] = padValue;
  }

  unsigned char* output = (unsigned char*)malloc(paddedLen);

  // mbedtls modifies the IV, so we use a copy
  unsigned char iv_copy[16];
  memcpy(iv_copy, aes_iv, 16);

  // Encrypt
  mbedtls_aes_crypt_cbc(&aes, MBEDTLS_AES_ENCRYPT, paddedLen, iv_copy, paddedInput, output);
  mbedtls_aes_free(&aes);

  // Base64 Encode
  size_t base64Len = 0;
  mbedtls_base64_encode(NULL, 0, &base64Len, output, paddedLen);
  unsigned char* base64Output = (unsigned char*)malloc(base64Len);
  mbedtls_base64_encode(base64Output, base64Len, &base64Len, output, paddedLen);

  String result = String((char*)base64Output);

  free(paddedInput);
  free(output);
  free(base64Output);

  return result;
}