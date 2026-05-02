//DHT11 sensor is connect to 3.3v, D4, and GND pins

// Wi‑Fi: copy `secrets.h.example` → `secrets.h` (gitignored).
#include "secrets.h"

//imports
#include "DHT.h" // DHT11 sensor
#include <WiFi.h> // WiFi connection
#include <WiFiClientSecure.h>
#include <HTTPClient.h> // HTTP send / recieve
#include <ArduinoJson.h> // JSON
#include <base64.h> // Base64
#include <cstring>
#include "esp_random.h"
#include "mbedtls/gcm.h"
#include "mbedtls/sha256.h"
#include <WebServer.h> // to listen to incoming request
#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <MAX30105.h>
#include "heartRate.h"
#include <LiquidCrystal_I2C.h>
#include <vector>
#include <numeric>
#include <algorithm>
#include <cmath>

WebServer server(80); // web server listening on port 80

// bisic configuration
const uint8_t DHT_PIN = 4; // define pin D4 for data
const uint8_t DHT_TYPE = DHT11; // define sensor type
constexpr uint8_t LED_PIN = 2; // LED pin D2 (on-board)
const uint8_t I2C_SDA_PIN = 21; // safe default I2C SDA
const uint8_t I2C_SCL_PIN = 22; // safe default I2C SCL
const uint8_t MQ135_PIN = 34; // analog input only pin
const uint8_t SOUND_PIN = 35; // analog input only pin
const uint8_t LIGHT_PIN = 32; // analog input capable pin
const uint8_t UV_PIN = 33; // analog input capable pin
const long SERIAL_BAUD = 115200; // changed to ESP32 standard

// Wi-Fi mode
wifi_mode_t wifi_mode = WIFI_STA; // WiFi connection type

// Server config — use https:// with Flask ssl_context='adhoc' (self-signed).
const String server_url = "https://192.168.12.202:8888";

// Secrets — UTF‑8 MASTER_ENCRYPTION_KEY must match Flask .env (SHA‑256 expands to AES‑256).
static const char *MASTER_ENC_SECRET_UTF8 = "0123456789123456";
static const char *INGEST_API_KEY_STR = "dev-ingest-change-me";  // Same as INGEST_API_KEY

// Sensor
DHT dht(DHT_PIN, DHT_TYPE); // Create sensor object
Adafruit_MPU6050 mpu6050;
MAX30105 max30102;
LiquidCrystal_I2C lcd(0x27, 16, 2);
bool mpu_ready = false;
bool max30102_ready = false;
bool lcd_ready = false;

// timing settings
const unsigned long READING_TIME_INTERVAL = 2000; //DHT11 hardware limit is 1Hz, increased to 2 seconds to decrease DHT components generating heat
const unsigned long SERIAL_TIME_INTERVAL = 2000; //serial print interval in ms, matches reading time interval
const unsigned long LED_BLINK_TIME_INTERVAL = 1000; //LED blink interval in ms
const unsigned long MPU_SAMPLE_INTERVAL_MS = 40; // high-rate movement polling
const unsigned long MAX_SAMPLE_INTERVAL_MS = 40; // high-rate PPG polling
const unsigned long ANALOG_SAMPLE_INTERVAL_MS = 250; // medium-rate analog polling
const unsigned long LCD_CYCLE_MS = 3000u; // 1602: rotate Temp/Hum vs SpO₂/HR vs VOC/Noise
const int CALIBRATION_SAMPLES = 10; // number of samples required to complete calibration

//Thresholds for temperature and humidity
float led_on_temperature = 30;
float led_on_humidity = 70;
float led_blink_temperature = 15;
float led_blink_humidity = 30;

float raw_temperature = 0; // Unfiltered hardware reading
float raw_humidity = 0; 
float last_known_ground_temp = 0; // user input ground truth/reference value, will use to apply offset
float last_known_ground_humid = 0; 
float temperature = 0; // after noise reduction/EMA filtering and offset application
float humidity = 0; // 
float temperature_f = 0; // in farenheit
float gyro_variance = 0;
float voc_level = 0;
float ambient_noise = 0;
float light_level = 0;
float uv_level = 0;
int heart_rate = 0;
int spo2 = 0;
unsigned long current_time = 0;
unsigned long last_post_time = 0;
unsigned long last_successful_sensor_read = 0;
unsigned long last_wifi_retry_time = 0;
int upload_interval = 10 * 1000; // 10 seconds
bool encrypt_flag = true;
std::vector<float> gyro_mag_window;
const size_t GYRO_WINDOW_SIZE = 20;
unsigned long last_beat_time = 0;
float bpm_rolling = 0;

// Measured against a digital thermometer on Feb 13th
const float FACTORY_TEMP_OFFSET = 0.5; 
const float FACTORY_HUMID_OFFSET = 0.0; 

// Start the global offsets with the factory bias instead of 0
float temp_offset = FACTORY_TEMP_OFFSET; 
float humid_offset = FACTORY_HUMID_OFFSET;

// Buffers for Calibration
std::vector<float> temp_buffer;
std::vector<float> humid_buffer;

bool plotter_mode = false; // Disables the text UI to stream CSV-formatted data for the Arduino Serial Plotter
bool monitoring_active = true; //monitoring active / inactive

// some boards have active high and some active low for LED
// this was added so more intuitive in program, can flip settings here
const int LED_ON = HIGH; 
const int LED_OFF = LOW; 

// for multi-point calibration
struct CalPoint {
  float raw;
  float truth;
};

std::vector<CalPoint> temp_cal_curve; // Stores all user-entered calibration points

// groups statistic metrics under one container
struct Stats {
  float mean;
  float stdDev;
};

// Enforces distinct system phases and prevents overlapping logic execution
enum SystemState {
  CALIBRATING,
  MONITORING
};
SystemState current_state = CALIBRATING; // start in calibration to build stable baseline before monitoring


void setup() {
  Serial.begin(SERIAL_BAUD); //init serial communication
  initWiFi(); // Init wifi connection
  // Reduces the serial timeout to 10ms. This prevents readStringUntil() from blocking the main sensor loop 
  Serial.setTimeout(10); 
  dht.begin(); // Init sensor and prep for readings
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

  if (mpu6050.begin()) {
    mpu6050.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu6050.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu6050.setFilterBandwidth(MPU6050_BAND_21_HZ);
    mpu_ready = true;
    Serial.println("MPU6050 detected.");
  } else {
    Serial.println("MPU6050 not found.");
  }

  if (max30102.begin(Wire, I2C_SPEED_FAST)) {
    // Recommended baseline config for MAX30102.
    max30102.setup(0x1F, 4, 2, 200, 411, 16384);
    max30102_ready = true;
    Serial.println("MAX30102 detected.");
  } else {
    Serial.println("MAX30102 not found.");
  }

  lcd.init();
  lcd.backlight();
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print("Sleep Monitor");
  lcd.setCursor(0, 1);
  lcd.print("Booting...");
  lcd_ready = true;

  pinMode(LED_PIN, OUTPUT); //activate LED pin
  digitalWrite(LED_PIN, LED_OFF); // start with light off

  // route api endpoints
  server.on("/health", HTTP_GET, get_health);
  server.on("/sensor", HTTP_GET, get_sensor);
  server.on("/config", HTTP_POST, post_config);
  server.on("/push-now", HTTP_POST, post_pushnow);

  server.begin();
  Serial.println("HTTP Server started.");
}

void loop() {
   // WebServer listen for incoming requests (/health, /push-now, etc.), no delay
  server.handleClient(); 

  current_time = millis(); 

  handle_wifi_reconnect(current_time);
  handleSensors(current_time);
  //handleSerialOutput(current_time);
  handleLED(current_time);
  handleUserInput();

  if (current_time - last_post_time >= upload_interval) {
    // Just verify we actually have WiFi before trying to POST
    if (WiFi.status() == WL_CONNECTED && current_state == MONITORING) { 
      post_data();
    }
    last_post_time = current_time; // Reset stopwatch  
  }
}


void handleSensors(unsigned long current_time) {
  static unsigned long previous_dht_time = 0;
  static unsigned long previous_analog_time = 0;
  static unsigned long previous_mpu_time = 0;
  static unsigned long previous_max_time = 0;

  if (monitoring_active && (current_time - previous_mpu_time >= MPU_SAMPLE_INTERVAL_MS)) {
    previous_mpu_time = current_time;
    if (mpu_ready) {
      sensors_event_t accel;
      sensors_event_t gyro;
      sensors_event_t temp;
      mpu6050.getEvent(&accel, &gyro, &temp);

      float gyro_mag = sqrt(
        (gyro.gyro.x * gyro.gyro.x) +
        (gyro.gyro.y * gyro.gyro.y) +
        (gyro.gyro.z * gyro.gyro.z)
      );

      gyro_mag_window.push_back(gyro_mag);
      if (gyro_mag_window.size() > GYRO_WINDOW_SIZE) {
        gyro_mag_window.erase(gyro_mag_window.begin());
      }

      if (gyro_mag_window.size() > 1) {
        Stats gyro_stats = calculateStats(gyro_mag_window);
        gyro_variance = gyro_stats.stdDev * gyro_stats.stdDev;
      }
      last_successful_sensor_read = current_time;
    }
  }

  if (monitoring_active && (current_time - previous_max_time >= MAX_SAMPLE_INTERVAL_MS)) {
    previous_max_time = current_time;
    if (max30102_ready) {
      max30102.check();
      while (max30102.available()) {
        long ir_value = max30102.getIR();
        long red_value = max30102.getRed();

        if (checkForBeat(ir_value)) {
          unsigned long beat_delta = current_time - last_beat_time;
          last_beat_time = current_time;
          if (beat_delta > 0) {
            float bpm = 60.0f / (beat_delta / 1000.0f);
            if (bpm > 30.0f && bpm < 220.0f) {
              bpm_rolling = (0.8f * bpm_rolling) + (0.2f * bpm);
              heart_rate = static_cast<int>(bpm_rolling + 0.5f);
            }
          }
        }

        if (ir_value > 1000 && red_value > 1000) {
          float ratio = static_cast<float>(red_value) / static_cast<float>(ir_value);
          spo2 = static_cast<int>(constrain(110.0f - (25.0f * ratio), 70.0f, 100.0f));
        }

        max30102.nextSample();
      }
      last_successful_sensor_read = current_time;
    }
  }

  if (monitoring_active && (current_time - previous_analog_time >= ANALOG_SAMPLE_INTERVAL_MS)) {
    previous_analog_time = current_time;
    // 12-bit ADC values are retained as raw engineering values for server-side analysis.
    voc_level = static_cast<float>(analogRead(MQ135_PIN));
    ambient_noise = static_cast<float>(analogRead(SOUND_PIN));
    light_level = static_cast<float>(analogRead(LIGHT_PIN));
    uv_level = static_cast<float>(analogRead(UV_PIN));
    last_successful_sensor_read = current_time;
  }

  // proceed only if the hardware interval has fully elapsed
  if (monitoring_active && (current_time - previous_dht_time >= READING_TIME_INTERVAL)) {
    previous_dht_time = current_time;
    
    // get raw data
    float raw_t = dht.readTemperature();
    float raw_h = dht.readHumidity();

    // Check for hardware failure
    if (isnan(raw_t) || isnan(raw_h)) return;  // Keep old values if read fails to prevent crashes
    last_successful_sensor_read = current_time;

    if (current_state == CALIBRATING) {
      // Store new raw data in buffer
      temp_buffer.push_back(raw_t);
      humid_buffer.push_back(raw_h);

      // Transition to monitoring after acquiring a sufficient baseline (10 samples)
      if (temp_buffer.size() >= CALIBRATION_SAMPLES) {
        finalizeCalibration(); // Calculate Offsets
        current_state = MONITORING; // Switch Mode
      }
    } 
    else if (current_state == MONITORING) {
      // update for graphing
      raw_temperature = raw_t;
      raw_humidity = raw_h;

      // update rolling window buffers
      temp_buffer.push_back(raw_t);
      humid_buffer.push_back(raw_h);
      if (temp_buffer.size() > CALIBRATION_SAMPLES) {
        temp_buffer.erase(temp_buffer.begin());
        humid_buffer.erase(humid_buffer.begin());
      }

      // hardware calibration
      // Calculate proposed values by applying correction factors/offset input from reference sources
      float proposed_t = applyReferenceOffset(raw_t);
      // Update the global temp_offset so the UI is accurate
      temp_offset = proposed_t - raw_t;

      // Use the flat global offset for humidity. Calibration curve not set up for humidity
      float proposed_h = raw_h + humid_offset;

      // Noise filtering
      temperature = applyNoiseFilter(proposed_t, temperature, temp_buffer, 0.15);
      humidity    = applyNoiseFilter(proposed_h, humidity, humid_buffer, 0.50);
      
      temperature_f = (temperature * 9.0 / 5.0) + 32.0;
    }
  }
}

// Calibration runs once when enough data is collected. Takes  and sets the offset.
void finalizeCalibration() {
  float avg_t = removeOutliersAndAverage(temp_buffer);
  float avg_h = removeOutliersAndAverage(humid_buffer);

  // average as baseline once calibration finished
  temperature = avg_t + temp_offset;
  humidity = avg_h + humid_offset;
}

// Z-score outlier removal - removes data points that are > 2 standard deviations away.
float removeOutliersAndAverage(std::vector<float> &data) {
  if (data.empty()) return 0;
  if (data.size() < 3) return calculateStats(data).mean; // Too small to filter outliers

  Stats stats = calculateStats(data);
  
  // If StdDev is 0 (all values identical), just return the mean
  if (stats.stdDev == 0) return stats.mean;

  float clean_sum = 0;
  int clean_count = 0;

  for (float val : data) {
    // Calculate Z-Score
    float z_score = abs(val - stats.mean) / stats.stdDev;

    // Keep points within 2 Standard deviations (95% of data)
    if (z_score <= 2.0) {
      clean_sum += val;
      clean_count++;
    }
  }

  if (clean_count == 0) return stats.mean; // Protect from divide by zero risk
  return clean_sum / clean_count;
}

// Calculates mean and standard deviation
Stats calculateStats(std::vector<float> &data) {
  Stats result = {0, 0};
  if (data.empty()) return result;

  //  Mean
  float sum = 0;
  for (float val : data) sum += val;
  result.mean = sum / data.size();

  // Variance
  float sumSqDev = 0;
  for (float val : data) {
    sumSqDev += pow(val - result.mean, 2);
  }
  float variance = sumSqDev / data.size();
  
  // Std Dev
  result.stdDev = sqrt(variance);
  
  return result;
}

// Multipoint reference offset
// Finds where the raw reading falls on the calibration curve and computes the exact offset.
float applyReferenceOffset(float raw_reading) {
  // If no points entered yet, trust the raw reading + global temp_offset if user hasn't established curve
  if (temp_cal_curve.empty()) return raw_reading + temp_offset;

  // If only 1 point entered, use it as a flat single-point offset
  if (temp_cal_curve.size() == 1) {
    float single_offset = temp_cal_curve[0].truth - temp_cal_curve[0].raw;
    return raw_reading + single_offset;
  }

  // extraploation if the reading is colder than our coldest data point
  if (raw_reading <= temp_cal_curve.front().raw) {
    float offset = temp_cal_curve.front().truth - temp_cal_curve.front().raw;
    return raw_reading + offset;
  }
  
  // extrapolation if the reading is hotter than our hottest data point
  if (raw_reading >= temp_cal_curve.back().raw) {
    float offset = temp_cal_curve.back().truth - temp_cal_curve.back().raw;
    return raw_reading + offset;
  }

  // Find the two points the reading falls between
  for (size_t i = 0; i < temp_cal_curve.size() - 1; i++) {
    if (raw_reading >= temp_cal_curve[i].raw && raw_reading <= temp_cal_curve[i+1].raw) {
      float x0 = temp_cal_curve[i].raw;   // Colder raw
      float y0 = temp_cal_curve[i].truth; // Colder truth
      float x1 = temp_cal_curve[i+1].raw; // Hotter raw
      float y1 = temp_cal_curve[i+1].truth; // Hotter truth

      // Calculate the exact point on the line between them
      return y0 + (raw_reading - x0) * ((y1 - y0) / (x1 - x0));
    }
  }
  return raw_reading + temp_offset;
}

// Calculates the dynamic noise threshold and blends the new reading with the 
// old reading to eliminate jitter while preserving real changes.
float applyNoiseFilter(float proposed_val, float current_filtered_val, std::vector<float> &buffer, float min_floor) {
  // Calculate the dynamic threshold based on recent noise
  float noise_threshold = calculateDynamicThreshold(buffer, min_floor);
  
  // Calculate the difference to determine if this a jump or just static
  float difference = abs(proposed_val - current_filtered_val);
  
  // EMA
  float alpha;
  if (difference > noise_threshold) alpha = 0.8;  // Real change: High trust, move quickly
  else alpha = 0.05; // Noise: Low trust, move slowly to smooth it out
  
  // Apply the Exponential Moving Average formula
  return (proposed_val * alpha) + (current_filtered_val * (1.0 - alpha));
}

// Sets the noise threshold dynamically 
float calculateDynamicThreshold(std::vector<float> &data, float min_floor) {
  Stats stats = calculateStats(data);

  // threshold must be between min_floor and 2.0 (heuristically chosen)
  return constrain(stats.stdDev * 2.0, min_floor, 2.0);
}

// prints output interval determined by SERIAL_TIME_INTERVAL variable/setting
void handleSerialOutput(unsigned long current_time) {
  static unsigned long serial_prev_time = 0;
  if (current_time - serial_prev_time >= SERIAL_TIME_INTERVAL) {
    serial_prev_time = current_time;
    print_output();
  }
}

// Setup WiFi conection
void initWiFi() {
  WiFi.mode(wifi_mode);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  last_wifi_retry_time = millis();
  Serial.println("WiFi connect started (non-blocking).");
}

// Handles WiFi reconnect if disconnected
// Call before doing any WiFi data transfer
void handle_wifi_reconnect(unsigned long current_time) {
  static bool wifi_was_connected = false;

  if (WiFi.status() != WL_CONNECTED) {
    wifi_was_connected = false;
    // Only try to reconnect every 5 seconds so we don't spam the router
    if (current_time - last_wifi_retry_time >= 5000) {
      Serial.println("WiFi connection lost. Attempting to reconnect...");
      WiFi.disconnect();
      WiFi.reconnect();
      last_wifi_retry_time = current_time;
    }
    return;
  }

  if (!wifi_was_connected) {
    Serial.print("Connected to SSID: ");
    Serial.print(WiFi.SSID());
    Serial.print(" Local IP: ");
    Serial.println(WiFi.localIP());
    wifi_was_connected = true;
  }
}

// Retrieves temperature from sensor
float get_temperature() {
  return dht.readTemperature(); 
}

// Retrieves humidity from sensor
float get_humidity() {
  return dht.readHumidity(); // return sensor humidity reading
}

static void sha256_secret_to_aes256_key(unsigned char out32[32]) {
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts_ret(&ctx, 0);
  mbedtls_sha256_update_ret(
      &ctx,
      reinterpret_cast<const unsigned char *>(MASTER_ENC_SECRET_UTF8),
      strlen(MASTER_ENC_SECRET_UTF8));
  mbedtls_sha256_finish_ret(&ctx, out32);
  mbedtls_sha256_free(&ctx);
}

// AES-256-GCM envelope: Base64(nonce12 || tag16 || ciphertext) — matches utils.encrypt_at_rest / ingest paths.
String encrypt_transport_gcm(const String &plainUtf8) {
  const size_t n = plainUtf8.length();
  if (n == 0 || n > 220) {
    return String("");
  }

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
    if (mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, aesKey, 256) != 0) {
      break;
    }
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

// Posts HTTPS to /post-data: WiFiClientSecure + mandatory X-API-KEY + AES-256-GCM for biometric/VOC payloads.
void post_data() {
  Serial.println("Attempting to send data...");

  WiFiClientSecure client;
  client.setInsecure();

  HTTPClient http;
  String full_server_url = server_url + "/post-data";

  if (!http.begin(client, full_server_url)) {
    Serial.println("HTTPS connection init failed.");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-KEY", INGEST_API_KEY_STR);

  // Create json document
  StaticJsonDocument<3072> data_doc;

  // Required sensitive channels: always GCM before leaving the device (plaintext fallbacks omitted by design).
  data_doc["temperature"] = encrypt_transport_gcm(String(temperature));
  data_doc["humidity"] = encrypt_transport_gcm(String(humidity));
  data_doc["heart_rate"] = encrypt_transport_gcm(String(heart_rate));
  data_doc["spo2"] = encrypt_transport_gcm(String(spo2));
  data_doc["gyro_variance"] = encrypt_transport_gcm(String(gyro_variance, 4));
  data_doc["air_quality"] = encrypt_transport_gcm(String(voc_level, 2));  // MQ‑135 / VOC lane
  data_doc["ambient_noise"] = encrypt_transport_gcm(String(ambient_noise, 2));

  // Optional context (not classified with core vitals above)
  data_doc["ambient_light"] = String(light_level, 2);

  String payload;
  serializeJson(data_doc, payload); // Save json payload as string

  // Show data encryptioon process
  Serial.println("==============================================================================================================");
  Serial.print("Payload: ");
  Serial.println(payload);
  Serial.println("==============================================================================================================");

  int http_response_code = http.POST(payload); // Send data and save response

  // If response, show it
  if (http_response_code > 0) {
    Serial.print("Response: ");
    Serial.println(http.getString());
  } else { // No respoonse was received
    Serial.println("No response received from server.");
  }

  http.end(); // Close http
  Serial.println("Data transmission complete.");
}

// Generates output to print to console showing menu and sensor readings
void print_output() {
  if (plotter_mode) {
    // Print Temp Data
    Serial.print("Raw_Temp:"); Serial.print(raw_temperature); Serial.print(","); 
    Serial.print("Filtered_Temp:"); Serial.print(temperature); Serial.print(","); 
    
    // Print Humid Data
    Serial.print("Raw_Humid:"); Serial.print(raw_humidity); Serial.print(","); 
    Serial.print("Filtered_Humid:"); Serial.print(humidity);
  
    Serial.println(); 
    return;
  }
  
  // Status Banner
  if (current_state == CALIBRATING) {
    Serial.print("\n");
    Serial.println("CALIBRATING SENSOR...");
    Serial.print("Collecting Samples: ");
    Serial.print(temp_buffer.size());
    Serial.print(" / ");
    Serial.print(CALIBRATION_SAMPLES);
    Serial.print("\t");
    Serial.print((int)((float)temp_buffer.size() / CALIBRATION_SAMPLES * 100));
    Serial.println(" % complete.");
  }
  if (current_state == MONITORING) {
    Serial.print("\n\n\n\n\n"); //print empty lines to move old text up for cleaner output

    // print project info
    Serial.println("==============================================================================================");
    Serial.println("UMKC | COMP-SCI 5577 Internet of Things | Mini Project 2 | Sean Gupta & Elliott Eisele-Miller");
    Serial.println("==============================================================================================");

    if (monitoring_active) {
      Serial.println("MONITORING ACTIVE");
    } else if (!monitoring_active){
      Serial.println("MONITORING INACTIVE");
    }

    //print menu and options
    Serial.println("============================================ MENU ============================================");
    Serial.println("Change value input format: \"menu_option_number:new_value\"");
    Serial.print("1: Set temperature upper (LED on) threshold    | Current value: "); Serial.print(led_on_temperature); Serial.println(" °C");
    Serial.print("2: Set humidity upper (LED on) threshold       | Current value: "); Serial.print(led_on_humidity); Serial.println(" %");
    Serial.print("3: Set temperature lower (LED blink) threshold | Current value: "); Serial.print(led_blink_temperature); Serial.println(" °C");
    Serial.print("4: Set humidity lower (LED blink) threshold    | Current value: "); Serial.print(led_blink_humidity); Serial.println(" %");
    Serial.print("5: Start / stop monitoring toggle              | Current value: "); Serial.println(monitoring_active ? "True" : "False");
    Serial.print("6: Calibrate Temp (Input Reference Temp)       | Raw value: "); Serial.print(raw_temperature); Serial.println(" °C");
    Serial.print("7: Calibrate Humidity (Input Reference Humid)  | Raw value: "); Serial.print(raw_humidity); Serial.println(" %");
    Serial.println("8: Toggle Graph Mode (With Serial Plotter) with '8:1'");
    Serial.println("============================================ Data ============================================");
    //check if monitoring active and only print data if active
    if (monitoring_active) {
      Serial.print("Temp: "); Serial.print(temperature); Serial.print(" °C | "); Serial.print(temperature_f); Serial.print(" °F "); Serial.print("(Offset: "); Serial.print(temp_offset); Serial.print(" °C)"); Serial.print("\t");
      Serial.print("Humid: "); Serial.print(humidity); Serial.print(" % (Offset: "); Serial.print(humid_offset); Serial.println(" %)");
      // Serial.print("Noise Threshold: +/-"); Serial.print(temp_threshold); Serial.print(" C, +/-"); Serial.print(humid_threshold); Serial.println(" %");
    } else {
      Serial.println("Data Stream PAUSED.");
    }
  } 
}

void handleLED(unsigned long current_time) {
  if (!lcd_ready) {
    return;
  }

  static unsigned long last_cal_paint_ms = 0;

  // Calibration splash
  if (current_state != MONITORING) {
    if (current_time - last_cal_paint_ms < 500) {
      return;
    }
    last_cal_paint_ms = current_time;
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Calibrating..");
    lcd.setCursor(0, 1);
    String cal = String((int)temp_buffer.size()) + "/" + String(CALIBRATION_SAMPLES);
    lcd.print(cal.substring(0, 16));
    return;
  }

  if (!monitoring_active) {
    lcd.clear();
    lcd.setCursor(0, 0);
    lcd.print("Monitoring OFF ");
    lcd.setCursor(0, 1);
    lcd.print("(local idle)    ");
    return;
  }

  static unsigned long page_epoch_ms = 0;
  static uint8_t page = 0;
  static uint8_t last_drawn_page = 255;
  static unsigned long last_value_paint_ms = 0;

  if (page_epoch_ms == 0) {
    page_epoch_ms = current_time;
  }
  if (current_time - page_epoch_ms >= LCD_CYCLE_MS) {
    page = (page + 1) % 3;
    page_epoch_ms = current_time;
  }

  const bool page_changed = (last_drawn_page != page);
  const bool ttl_elapsed =
      page_changed || (current_time - last_value_paint_ms >= 450);

  if (!ttl_elapsed) {
    return;
  }
  last_drawn_page = page;
  last_value_paint_ms = current_time;

  lcd.clear();
  switch (page) {
    case 0:
      lcd.setCursor(0, 0);
      lcd.print("TempC  RH% raw");
      lcd.setCursor(0, 1);
      {
        String l1 =
            String(raw_temperature, 1)
            + " "
            + String(raw_humidity, 1);
        lcd.print(l1.substring(0, 16));
      }
      break;
    case 1:
      lcd.setCursor(0, 0);
      lcd.print("SpO2%   HR(bpm)");
      lcd.setCursor(0, 1);
      {
        String l1 = String(spo2) + "     " + String(heart_rate);
        lcd.print(l1.substring(0, 16));
      }
      break;
    case 2:
    default:
      lcd.setCursor(0, 0);
      lcd.print("VOC   Noise adc");
      lcd.setCursor(0, 1);
      {
        String l1 =
            String(static_cast<int>(voc_level))
            + " "
            + String(static_cast<int>(ambient_noise));
        lcd.print(l1.substring(0, 16));
      }
      break;
  }
}

void handleUserInput() {
  if (Serial.available() > 0) {
    String inputLine = Serial.readStringUntil('\n');
    inputLine.trim();
    if (inputLine.length() > 0) {
      process_input(inputLine);
    }
  }
}

void process_input(String input) {
  int colonIndex = input.indexOf(':'); //find colon index in string

  //check if input matches expected format
  if (colonIndex != -1) {
    String menu_option_number = input.substring(0, colonIndex); //get option number before separation char
    String new_value = input.substring(colonIndex + 1); //get new val from after separation char

    //remove white space
    menu_option_number.trim(); 
    new_value.trim(); 

    //switch case to handle which option the user chose
    switch (menu_option_number.toInt()) {
      case 1: // upper temp (LED on)
        led_on_temperature = new_value.toFloat(); //update value from user input
        break;
      
      case 2: // upper humidity (LED on)
        led_on_humidity = new_value.toFloat(); //update value from user input
        break;

      case 3: // lower temp (LED blink)
        led_blink_temperature = new_value.toFloat(); //update value from user input
        break;

      case 4: //lower humidity (LED blink)
        led_blink_humidity = new_value.toFloat(); //update value from user input
        break;

      case 5: // set monitoring active/inactive
        new_value.toLowerCase(); //convert input to lower case

        //validate input with different input options to cover cases
        if(new_value == "true" || new_value == "t" || new_value == "1"){
          monitoring_active = true; //update monitoring to true (active)
        } else if(new_value == "false" || new_value == "f" || new_value == "0"){
          monitoring_active = false; //update monitoring to false (inactive)
        } else {
          //advise user of error with input
          Serial.print("Error: Invalid input \"");
          Serial.print(new_value);
          Serial.println("\". Use True/False or T/F or 1/0.");
        } 
        break;

      case 6: // multipoint calibration
      {
         last_known_ground_temp = new_value.toFloat();
         
         // Record the current state (Raw vs Truth)
         temp_cal_curve.push_back({raw_temperature, last_known_ground_temp});
         
         // Sort the vector by raw temperature (coldest to hottest)
         std::sort(temp_cal_curve.begin(), temp_cal_curve.end(), [](const CalPoint& a, const CalPoint& b) {
             return a.raw < b.raw;
         });
         
         Serial.print("Recorded Calibration Point: Raw ");
         Serial.print(raw_temperature);
         Serial.print(" = Truth ");
         Serial.println(last_known_ground_temp);
         break;
      }

      case 7: // calibrate humid with reference
         last_known_ground_humid = new_value.toFloat();
         humid_offset = last_known_ground_humid - raw_humidity;
         humidity = last_known_ground_humid; 
         break;

      case 8: // Toggle graph mode
         plotter_mode = !plotter_mode; // Switch True/False
         break;

      default:
        Serial.println("ERROR: Invalid input"); //advice of error with input
        break;
    } 
  } 
} 

// ------------contains all API endpoint handlers---------------

// Define what happens when someone asks for /health
// return device status (e.g., { "ok": true, "uptime_s": 120 })
void get_health() {
  long uptime_s = millis() / 1000; 

  // If current time is more than 5000ms past the last read, the sensor timed out
  bool sensor_ok = (millis() - last_successful_sensor_read) < 5000;

  // check available ram
  uint32_t free_heap = ESP.getFreeHeap();

  bool system_ok = sensor_ok && (WiFi.status() == WL_CONNECTED);

  StaticJsonDocument<200> data_doc; 
  data_doc["ok"] = system_ok;
  data_doc["uptime_s"] = uptime_s;

  // Create a nested diagnostics object to keep it organized
  JsonObject diagnostics = data_doc.createNestedObject("diagnostics");
  diagnostics["sensor_working"] = sensor_ok;
  diagnostics["free_memory_bytes"] = free_heap;

  String json;
  serializeJson(data_doc, json);

  int http_code;
  if (system_ok){
    http_code = 200;
  } else {
    http_code = 500;
  }
  server.send(http_code, "application/json", json);
}

// listen for request asking ESP32 to read the DHT11 and returns the latest temperature and humidity
// as JSON. If encryption is enabled, fields are AES-256-GCM payloads (base64 of nonce||tag||ciphertext).
void get_sensor() {
  StaticJsonDocument<3072> data_doc;

  if (encrypt_flag){
    data_doc["temperature"] = encrypt_transport_gcm(String(temperature));
    data_doc["humidity"] = encrypt_transport_gcm(String(humidity));
    data_doc["heart_rate"] = encrypt_transport_gcm(String(heart_rate));
    data_doc["spo2"] = encrypt_transport_gcm(String(spo2));
    data_doc["gyro_variance"] = encrypt_transport_gcm(String(gyro_variance, 4));
    data_doc["voc_level"] = encrypt_transport_gcm(String(voc_level, 2));
    data_doc["ambient_noise"] = encrypt_transport_gcm(String(ambient_noise, 2));
    data_doc["light_level"] = encrypt_transport_gcm(String(light_level, 2));
    data_doc["uv_level"] = encrypt_transport_gcm(String(uv_level, 2));
  } else {
    data_doc["temperature"] = String(temperature);
    data_doc["humidity"] = String(humidity);
    data_doc["heart_rate"] = String(heart_rate);
    data_doc["spo2"] = String(spo2);
    data_doc["gyro_variance"] = String(gyro_variance, 4);
    data_doc["voc_level"] = String(voc_level, 2);
    data_doc["ambient_noise"] = String(ambient_noise, 2);
    data_doc["light_level"] = String(light_level, 2);
    data_doc["uv_level"] = String(uv_level, 2);
  }
  
  String payload;
  serializeJson(data_doc, payload); // Save json payload as string
  server.send(200, "application/json", payload);
}

// listen for new settings. Accepts JSON to update settings such as
// upload interval or encryption flag, and responds with the new config. 
void post_config() {
  // Check if the user actually sent a payload
  if (!server.hasArg("plain")) {
    server.send(400, "text/plain", "Error: No JSON payload sent");
    return;
  }
  // grab raw JSON string from incoming POST request
  String incoming_json = server.arg("plain");

  StaticJsonDocument<200> data_doc; // create JSON doc
  DeserializationError error = deserializeJson(data_doc, incoming_json);

  if (error) {
    server.send(400, "application/json", "{\"error\": \"Invalid JSON format\"}");
    return;
  }

  // update variables if keys are present
  if (data_doc.containsKey("upload_interval")){
    upload_interval = data_doc["upload_interval"].as<int>(); // convert JSON value to int and update global variable
    Serial.println("Upload interval updated to: " + String(upload_interval));
  }

  if (data_doc.containsKey("encrypt_flag")){
    encrypt_flag = data_doc["encrypt_flag"].as<bool>(); // convert JSON value to int and update global variable
    Serial.println("Encryption flag updated to: "+ String(encrypt_flag));
  }

  // reject if neither valid key sent
  if (data_doc["upload_interval"].isNull() && data_doc["encrypt_flag"].isNull()) {
    server.send(400, "application/json", "{\"error\": \"Invalid keys. Use 'upload_interval' (int) or 'encrypt_flag' (bool).\"}");
    return;
  }

  String response_json;
  data_doc.clear();
  data_doc["upload_interval_seconds"] = upload_interval / 1000 ;
  data_doc["encryption_flag"] = encrypt_flag;

  serializeJson(data_doc, response_json);

  server.send(200, "application/json", response_json);
}

// Immediately captures and uploads a DHT11 reading to the cloud server. 
void post_pushnow() { 
  post_data(); // Immediately run existing function to upload to Flask
  last_post_time = millis(); // Reset the 30-second stopwatch so it restarts the countdown from right now
  server.send(200, "text/plain", "Data pushed to cloud immediately."); 
}