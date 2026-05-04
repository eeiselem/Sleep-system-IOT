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

Minimum variables for ingest + encrypted fields: **`FLASK_SECRET_KEY`**, **`MASTER_ENCRYPTION_KEY`** (must match firmware), **`INGEST_API_KEY`** (must match firmware). OpenAI can stay empty if you do not use sleep-coach / LLM features.

Run the server:

```powershell
python app.py
```

Default: **HTTPS on port 8888** with Flask adhoc (self-signed) TLS — browsers will warn unless you trust the certificate. ESP32 uses `WiFiClientSecure` + `setInsecure()` against that setup.

### Cloudflare Tunnel (quick public HTTPS URL)

Use this when the ESP32 or a phone browser must reach your PC **without** LAN IP / self-signed cert hassles. **Cloudflare terminates TLS**; Flask runs **HTTP** locally as the tunnel origin.

1. Install **cloudflared** (e.g. [Cloudflare’s install docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/) or `winget install --id Cloudflare.cloudflared`).
   - **Windows:** if `cloudflared` is “not recognized” after install, **close and reopen the terminal**, or refresh PATH in the current session:
     `$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")`
     Or call the binary directly: `& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://127.0.0.1:8888`
2. In **`.env`**, set **`FLASK_USE_SSL=0`** (keep **`FLASK_PORT=8888`** unless you change both sides).
3. **Terminal A** — Flask on HTTP: `python app.py`
4. **Terminal B** — tunnel: `cloudflared tunnel --url http://127.0.0.1:8888`
5. Copy the printed **`https://….trycloudflare.com`** URL. In **`ESP32.ino`**, set **`server_url`** to that base (no trailing slash). Rebuild/flash.

**Notes:** Quick tunnels often get a **new hostname when you restart** `cloudflared` unless you set up a **named tunnel** in Cloudflare. Exposing Flask to the internet is only as safe as your app and secrets — treat it as a **demo**.

**If quick tunnel fails** with `Error unmarshaling QuickTunnel response` / `invalid character 'e' looking for beginning of value` / **500**: Cloudflare’s **account-less** quick API sometimes returns a non-JSON error (rate limits, transient outage, or a block). Try: **wait 30–60 minutes** and retry; turn off **VPN**; try another **network** (phone hotspot); upgrade **cloudflared** to the latest release; or use a **free Cloudflare account + named tunnel** ([docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/tunnel-guide/)) for a stable URL. For a class demo, **ngrok** (`ngrok http 8888`) is a common alternative.

### Firmware: which sketch to update (two boards)

| Who | File | Flash this when… |
|-----|------|-------------------|
| **You (environment kit)** | **`ESP32/ESP32.ino`** | Full stack: DHT/MQ/sound/light/UV/MPU/MAX301 + **`POST /post-data`**. Update **`server_url`**, **`INGEST_API_KEY_STR`**, **`MASTER_ENC_SECRET_UTF8`** to match **`.env`**. Wi‑Fi in **`ESP32/secrets.h`**. |
| **You (environment-only second board)** | **`ESP32/Environmental_Device.ino`** | Teammate kit: DHT, MQ‑135 (+ gas ADC), sound/light, UV on LCD, optional MPU6050 + **`POST /post-environment`** (same GCM + **`INGEST_API_KEY`** as **`/post-data`**). Temp/humidity/air on this board; vitals from **`biometric.ino`**. **Do not compile** in the same Arduino folder as **`ESP32.ino`** (see file header). |
| **Teammate (HR/SpO₂/HRV + MPU)** | **`ESP32/biometric.ino`** | **`POST /biometric`** only. Same **`INGEST_API_KEY_STR`** as **`.env`**. Same **`server_url`** base if both boards use the same tunnel/LAN. Keeps **`WiFiClientSecure` alive through `POST`** (needed for Cloudflare **`https://`**). |

**Both boards** must agree on **`INGEST_API_KEY`** / **`INGEST_API_KEY_STR`**. Sketches that use **`/post-data`** or **`/post-environment`** need **`MASTER_ENC_SECRET_UTF8`** aligned with **`MASTER_ENCRYPTION_KEY`**. **`biometric.ino`** uses AES‑128‑CBC keys in the sketch unless you set **`BIOMETRIC_AES_*`** in **`.env`** on the server.

**Server / `.env`:** After tunnel or key changes, restart **`python app.py`**. For tunnels use **`FLASK_USE_SSL=0`** and **`cloudflared --url http://127.0.0.1:8888`**.

## Dependencies (`requirements.txt`)

Installed via `pip install -r requirements.txt`:

- `python-dotenv`, `Flask`, `Flask-Bcrypt`, `Flask-Login`, `Flask-SQLAlchemy`, `openai`, `pycryptodome`, `pydantic`, `pyopenssl` (adhoc HTTPS)
