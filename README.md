# IoT Sleep / Environment Monitor

This project is a Flask + SQLite app with a dashboard and ESP32 ingest endpoints.

## Run locally (Windows PowerShell)

From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create env file:

```powershell
copy .env.example .env
```

Edit .env and set at least:

- FLASK_SECRET_KEY
- MASTER_ENCRYPTION_KEY
- INGEST_API_KEY

Start the app:

```powershell
python app.py
```

Open:

- https://127.0.0.1:8888

## ESP32 sketches in this repo

Wi-Fi once: copy ESP32/secrets.h.example to ESP32/secrets.h. Every sketch uses #include "../secrets.h" from its folder.

- Environment board: ESP32/Environmental Device/Environmental_Device.ino
  - Set server_url, MASTER_ENC_SECRET_UTF8 (must match .env MASTER_ENCRYPTION_KEY), and INGEST_API_KEY_STR (must match INGEST_API_KEY).
  - POST goes to /post-environment with per-field AES-256-GCM.
- Biometric board: ESP32/biometric/biometric.ino (do not open in the same Arduino project as the env sketch).
- Mock sender (optional): ESP32/mock_environment_post/mock_environment_post.ino
