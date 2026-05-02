# IoT Sleep / Environment Monitor

Flask backend with SQLite, ESP32 sensor ingest, and a web dashboard.

## Security (demo / course project)

This is **not hardened production software**. On a trusted LAN or for grading, relaxed defaults are okay. Treat **Wi‑Fi** and **paid API keys** as the things worth hiding: keep **`ESP32/secrets.h`** (copy from `secrets.h.example`) and **`.env`** out of git (see `.gitignore`). Commit only **`.env.example`** and **`ESP32/secrets.h.example`**. Put **`OPENAI_API_KEY`** and anything costly in **`.env`** locally, not in source. Committing an empty `server.db` is optional; ignored by default to avoid stale binary noise.

## Python setup (venv)

From the project root (`IoTs-Mini-Project-4-main`):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy secrets template and edit:

```powershell
copy .env.example .env
# Edit .env in an editor — set FLASK_SECRET_KEY, MASTER_ENCRYPTION_KEY, INGEST_API_KEY at minimum.
```

Minimum variables for ingest + encrypted fields: **`FLASK_SECRET_KEY`**, **`MASTER_ENCRYPTION_KEY`** (must match firmware), **`INGEST_API_KEY`** (must match firmware). Weather and OpenAI can stay empty if you do not use those routes/features.

Run the server:

```powershell
python app.py
```

Default: **HTTPS on port 8888** with Flask adhoc (self-signed) TLS — browsers will warn unless you trust the certificate. ESP32 uses `WiFiClientSecure` + `setInsecure()` against that setup.

ESP32: **`ESP32/ESP32.ino`**. Copy **`ESP32/secrets.h.example`** → **`ESP32/secrets.h`** and set Wi‑Fi. Match **`server_url`**, **`INGEST_API_KEY_STR`**, and **`MASTER_ENC_SECRET_UTF8`** to Flask **`.env`** as needed. Build with **Arduino IDE** (ESP32 core) or **PlatformIO**.

## Dependencies (`requirements.txt`)

Installed via `pip install -r requirements.txt`:

- `python-dotenv`, `Flask`, `Flask-Bcrypt`, `Flask-Login`, `Flask-SQLAlchemy`, `requests`, `openai`, `pycryptodome`, `pydantic`, `pyopenssl` (adhoc HTTPS)
