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

Edit `.env` and set at least:

- `FLASK_SECRET_KEY`
- `MASTER_ENCRYPTION_KEY`
- `INGEST_API_KEY`

Start the app:

```powershell
python app.py
```

Open:

- `https://127.0.0.1:8888`

## ESP32 sketches in this repo

- Environment board: `ESP32/Environmental Device/Environmental_Device.ino`
- Biometric board: `ESP32/biometric/biometric.ino`

Both boards should use the same ingest API key value as `.env`.
