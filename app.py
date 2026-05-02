import os
import time
import json
import hashlib
import secrets
import random
import threading
import requests
from datetime import date, datetime, timedelta, timezone
from openai import OpenAI
from flask import Flask, request, render_template, redirect, url_for, session
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
from models import ReadingBase, SubjectiveSleepReviewIn
from pydantic import ValidationError

from db import db
from schemas.user import User
from schemas.reading import Reading
from schemas.sleep_session import SleepSession
from schemas.micro_arousal_event import MicroArousalEvent
from schemas.subjective_sleep_review import SubjectiveSleepReview
from sleep_metrics import compute_sleep_readiness_for_session
from crud import reading
from crud.reading import EVENT_LOG_ALLOWED_METRICS, EVENT_LOG_METRIC_LABELS
from utils import get_current_utc_time, ingest_field_plaintext

# Metrics available in the dashboard “breach” event log (autonomous thresholds / anomalies).
EVENT_LOG_BREACH_METRICS = frozenset({
    "heart_rate",
    "spo2",
    "ambient_noise",
    "air_quality",
    "gyro_variance",
})
EVENT_LOG_BREACH_LABELS = {
    k: EVENT_LOG_METRIC_LABELS[k]
    for k in EVENT_LOG_BREACH_METRICS
}
from logic import (
    _latest_sleep_session_ended_on,
    default_micro_arousal_ctx,
    evaluate_sleep_state,
    get_sleep_session_resolution_context,
    get_user_sleep_consciousness_state,
    micro_arousal_tick,
    run_daily_optimal_band_updates,
    snapshot_sleep_tracking,
)

# load environment variables from the .env file
load_dotenv()

# Biological / proposal defaults (IEC-style sleep-environment guidance + luminaire night corridor).
SCI_TEMP_BAND_C_MIN = 15.0
SCI_TEMP_BAND_C_MAX = 19.0
SCI_HUMIDITY_PCT_MIN = 40.0
SCI_HUMIDITY_PCT_MAX = 60.0
SCI_LUX_MIN = 0.0
SCI_LUX_MAX = 10.0
# Absolute MQ‑135 VOC / air‑quality excursion (index units — tune via env).
try:
    MQ135_SAFE_INDEX_MAX = float(os.getenv("MQ135_SAFE_INDEX_MAX", "420"))
except ValueError:
    MQ135_SAFE_INDEX_MAX = 420.0

app = Flask(__name__)
# encrypt the session cookie with secret key
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Tell SQLAlchemy where to build the physical SQLite file
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///server.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# connect Flask to the database through SQLAlchemy
db.init_app(app)


# Initialize the security tools
bcrypt = Bcrypt(app)  # encrypts passwords and checks them during login
login_manager = LoginManager(app)  # tracks who is logged in.
login_manager.login_view = 'login'


def to_utc_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat_z(value):
    utc_value = to_utc_datetime(value)
    if utc_value is None:
        return None
    return utc_value.isoformat().replace("+00:00", "Z")


def parse_local_datetime_to_utc(datetime_str):
    if not datetime_str:
        return None
    naive_local = datetime.fromisoformat(datetime_str)
    local_tz = datetime.now().astimezone().tzinfo
    local_aware = naive_local.replace(tzinfo=local_tz)
    return local_aware.astimezone(timezone.utc)


def ensure_reading_columns():
    existing_columns = {
        row[1]
        for row in db.session.execute(
            db.text("PRAGMA table_info(readings)")
        ).fetchall()
    }
    required_columns = {
        "air_quality": "TEXT",
        "ambient_noise": "TEXT",
        "ambient_light": "TEXT",
        "heart_rate": "TEXT",
        "spo2": "TEXT",
        "gyro_variance": "TEXT",
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            db.session.execute(
                db.text(f"ALTER TABLE readings ADD COLUMN {column_name} {column_type}")
            )

    db.session.commit()


def ensure_user_config_columns():
    existing_columns = {
        row[1]
        for row in db.session.execute(
            db.text("PRAGMA table_info(users)")
        ).fetchall()
    }
    required_columns = {
        "cfg_temp_min": "REAL",
        "cfg_temp_max": "REAL",
        "cfg_noise_limit": "REAL",
        "cfg_wake_time": "TEXT",
        "cfg_wake_days": "TEXT",
        "cfg_guardrail_temp_f_min": "REAL",
        "cfg_guardrail_temp_f_max": "REAL",
        "cfg_optimal_band_f_min": "REAL",
        "cfg_optimal_band_f_max": "REAL",
        "cfg_override_optimal_band": "INTEGER",
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            db.session.execute(
                db.text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}")
            )

    db.session.commit()


#  active app context to interact with the database
with app.app_context():
    # Looks at schemas. If server.db doesn't have those tables, builds them
    db.create_all()
    ensure_reading_columns()
    ensure_user_config_columns()

    # checks for admin user. If not found creates them
    if not User.query.filter_by(username="admin").first():
        hashed_pw = bcrypt.generate_password_hash('admin').decode('utf-8')
        new_admin = User(username="admin", password_hash=hashed_pw, role="Admin")
        db.session.add(new_admin)
        db.session.commit()
        print("--- Admin Account Created: Use 'admin' and 'admin' ---")

    if not User.query.filter_by(username="testuser").first():
        hashed_pw = bcrypt.generate_password_hash('password123').decode('utf-8')
        new_test = User(username="testuser", password_hash=hashed_pw, role="User")
        db.session.add(new_test)
        db.session.commit()


# dictionary linking User IDs to the exact time they last clicked a button.
user_activity = {}


# Runs before any webpage for a user is loaded
# if the user is logged in, update their last active time in user_activity dict
@app.before_request
def track_user_activity():
    if current_user.is_authenticated:
        user_activity[current_user.id] = get_current_utc_time()


# finds user in the database by unique ID and load info into 'current_user'
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.route('/login', methods=['GET', 'POST'])
def login():
    # POST means the user clicked the "Submit" button on the form
    if request.method == 'POST':
        # Grab the text they typed into the username and password boxes
        username = request.form.get('username')
        password = request.form.get('password')

        # grab user from the database with that username
        # If no user has that username, this will be None
        user = User.query.filter_by(username=username).first()

        # If user exists, see if their typed password matches the database hash
        # login and send them to the dashboard/index
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            print("Invalid login attempt")

    # Failed login or GET means user just navigated to /login
    return render_template('login.html')


@app.route('/logout')
def logout():
    logout_user()
    print("User logged out successfully.")
    return redirect(url_for('login'))  # return back to the login screen.


# dashboard page. This is the main page users see when they log in.
@app.route('/dashboard')
@login_required
def index():
    # Fetch the data but don't unpack
    latest_data = reading.read_latest()

    if latest_data is None:
        latest_data = {
            "timestamp": get_current_utc_time(),
            "temperature": "N/A",
            "humidity": "N/A",
            "air_quality": "N/A",
            "ambient_noise": "N/A",
            "ambient_light": "N/A",
            "heart_rate": "N/A",
            "spo2": "N/A",
            "gyro_variance": "N/A",
        }
    else:
        for field_name in (
            "air_quality",
            "ambient_noise",
            "ambient_light",
            "heart_rate",
            "spo2",
            "gyro_variance",
        ):
            if latest_data[field_name] is None:
                latest_data[field_name] = "N/A"
    latest_data["timestamp"] = to_utc_datetime(latest_data["timestamp"])

    # check if user is searching for something specific.
    # If so, grab the search rules from the browser session.
    start_datetime_str = session.get("start_datetime") or ""
    end_datetime_str = session.get("end_datetime") or ""
    event_metric = (session.get("event_metric") or "").strip()
    if event_metric and event_metric not in EVENT_LOG_BREACH_METRICS:
        session.pop("event_metric", None)
        session.pop("event_threshold", None)
        event_metric = ""
    event_threshold_raw = session.get("event_threshold")
    event_direction = session.get("event_direction") or "below"

    search_results = None
    event_log_summary = None
    event_metric_highlight = ""
    breach_table_rows = None
    event_log_error = session.pop("event_log_error", None)

    def _has_valid_breach_query():
        if event_metric not in EVENT_LOG_BREACH_METRICS:
            return False
        if event_threshold_raw in (None, "") or not str(event_threshold_raw).strip():
            return False
        return True

    if _has_valid_breach_query():
        start_date = parse_local_datetime_to_utc(
            start_datetime_str or None
        )
        end_date = parse_local_datetime_to_utc(end_datetime_str or None)

        thr_clean = str(event_threshold_raw).strip()
        direction = event_direction if event_direction in ("above", "below") else "below"

        search_rows = reading.read_search(
            metric=event_metric,
            threshold=thr_clean,
            direction=direction,
            start_date=start_date,
            end_date=end_date,
            threshold_temperature=None,
        )
        lbl = EVENT_LOG_BREACH_LABELS[event_metric]
        event_log_summary = (
            f"{lbl} — values {direction} threshold {thr_clean} "
            f"(optional local window applied; default scope: last 7 UTC days)"
        )
        event_metric_highlight = event_metric
        search_results = search_rows if search_rows is not None else []
        breach_table_rows = []
        for r in search_results:
            breach_table_rows.append(
                {
                    "id": r.id,
                    "timestamp": r.timestamp,
                    "value": getattr(r, event_metric, None),
                }
            )

    # Build dhashboard.html by injecting all this data into the template.
    return render_template(
        "dashboard.html",
        user=current_user,
        current_timestamp=latest_data["timestamp"],
        current_timestamp_utc=utc_isoformat_z(latest_data["timestamp"]),
        current_temperature=latest_data["temperature"],
        current_humidity=latest_data["humidity"],
        current_heart_rate=latest_data["heart_rate"],
        current_spo2=latest_data["spo2"],
        current_ambient_noise=latest_data["ambient_noise"],
        current_ambient_light=latest_data.get("ambient_light", "N/A"),
        current_voc=latest_data["air_quality"],
        search_results=search_results,
        breach_table_rows=breach_table_rows,
        event_log_summary=event_log_summary,
        event_log_breach_labels=EVENT_LOG_BREACH_LABELS,
        event_metric_highlight=event_metric_highlight,
        event_log_error=event_log_error,
    )


# info dashboard page.
@app.route('/info')
@login_required
def info_dashboard():
    start_time = time.perf_counter()
    total_records = Reading.query.count()
    # how long to count all records in the database
    query_efficiency = (time.perf_counter() - start_time) * 1000

    # determine active users: Count how many users clicked a link in last 5 min
    cutoff_time = get_current_utc_time() - timedelta(minutes=5)
    active_count = sum(1 for last_seen in user_activity.values() if last_seen > cutoff_time)

    # Calculate Database Throughput: Count exactly how many records the ESP32 uploaded in the last 60 seconds.
    one_minute_ago = get_current_utc_time() - timedelta(minutes=1)
    recent_count = Reading.query.filter(Reading.timestamp >= one_minute_ago).count()
    throughput = f"{recent_count} records/min"

    # for encryption visual
    latest = Reading.query.order_by(Reading.id.desc()).first() 
    sample_payload = {
        "temp": latest.temperature if latest else "N/A",
        "hum": latest.humidity if latest else "N/A",
        "encrypted_blob": "AES_ENCRYPTED_DATA_DETECTED" if latest else "WAITING..."
    }

    # Inject all this diagnostic data into the info.html template.
    return render_template(
        "info.html",
        user=current_user,
        active_sessions=active_count,
        total_records=total_records,
        query_efficiency=f"{query_efficiency:.2f}ms",
        throughput=throughput,
        latest_data=sample_payload,
        user_count=User.query.count(),
        all_users=User.query.all() # Hands a list of every user to the HTML
    )


@app.route('/submit-event-log-search', methods=['POST'])
def event_log_search():
    metric = (request.form.get("metric") or "").strip()
    threshold = request.form.get("threshold")
    direction = (request.form.get("direction") or "below").strip().lower()
    start = request.form.get("start_time") or ""
    end = request.form.get("end_time") or ""

    def _reject(msg: str):
        session["event_log_error"] = msg
        session["event_metric"] = metric
        session["event_threshold"] = threshold or ""
        session["event_direction"] = direction if direction in ("above", "below") else "below"
        session["start_datetime"] = start
        session["end_datetime"] = end
        return redirect(url_for("index"))

    if metric not in EVENT_LOG_BREACH_METRICS:
        return _reject(
            "Select one metric: heart rate, SpO₂, ambient noise, VOC/gas, or gyro variance."
        )
    if threshold in (None, "") or not str(threshold).strip():
        return _reject("Enter a numeric threshold.")
    try:
        float(str(threshold).strip())
    except ValueError:
        return _reject("Threshold must be a valid number.")

    if direction not in ("above", "below"):
        direction = "below"

    session.pop("threshold_temperature", None)
    session.pop("direction", None)
    session.pop("event_log_error", None)

    session["event_metric"] = metric
    session["event_threshold"] = str(threshold).strip()
    session["event_direction"] = direction
    session["start_datetime"] = start
    session["end_datetime"] = end

    return redirect(url_for("index"))


@app.route('/submit-temperature-search', methods=['POST'])
def temperature_search():
    """Older dashboard links: migrate into event-log session keys."""
    thresh = request.form.get("threshold_temperature")
    direction = request.form.get("direction") or "above"
    start = request.form.get("start_time") or ""
    end = request.form.get("end_time") or ""
    session["event_metric"] = ""
    session["event_threshold"] = thresh
    session["event_direction"] = direction
    session["threshold_temperature"] = thresh
    session["direction"] = direction
    session["start_datetime"] = start
    session["end_datetime"] = end
    return redirect(url_for("index"))


def _pop_event_log_session_keys():
    session.pop("event_metric", None)
    session.pop("event_threshold", None)
    session.pop("event_direction", None)
    session.pop("start_datetime", None)
    session.pop("end_datetime", None)
    session.pop("threshold_temperature", None)
    session.pop("direction", None)
    session.pop("event_log_error", None)


@app.route('/clear-event-log-search', methods=['POST'])
def clear_event_log_search():
    _pop_event_log_session_keys()
    return redirect(url_for("index"))


@app.route('/clear-temperature-search', methods=['POST'])
def clear_search():
    _pop_event_log_session_keys()
    return redirect(url_for("index"))


user_activity = {}
total_records_cache = None
sim_room_lock = threading.Lock()
simulated_room_state = {
    "room_id": "bedroom-sim-01",
    "state": "initializing",
    "occupied": True,
    "sleep_mode_active": False,
    "sleep_onset_confirmed": False,
    "sleep_signal_score": 0,
    "last_drift_detected": [],
    # Presentation toggles only (writable via POST /api/dev/simulation). When set, badges show
    # intervention activity without relying on sensor baselines reaching drift thresholds.
    "dev_simulation": {
        "force_high_temperature": False,
        "force_high_noise": False,
        "force_low_temperature": False,
        "force_voc_spike": False,
    },
    "simulated_hardware": {
        "cooling": False,
        "heater": False,
        "white_noise": False,
        "fan": False,
        "air_filtration_high_fan": False,
    },
    # Proposal defaults: circadian nighttime ambient target 0–10 lm expressed as nominal + limits.
    "lighting": {
        "state": "ambient",
        "brightness_percent": 8,
        "lumens_nominal": 5,
        "lumens_range_lm": {"min": 0, "max": 10},
    },
    # Thermal default midpoint of 15–19 °C documented band (°C).
    "hvac": {"mode": "idle", "target_temperature_c": 17.0},
    "ventilation": {"mode": "normal", "fan_percent": 20},
    "environment": {
        "temperature_c": None,
        "humidity_percent": None,
        "ambient_noise_db": None,
        "voc_index": None,
    },
    # Strict scientific defaults — midpoints of allowed bands at control-loop init (°C / %RH / lux).
    "baselines": {
        "temperature_c": (SCI_TEMP_BAND_C_MIN + SCI_TEMP_BAND_C_MAX) / 2.0,
        "temperature_band_c": {"min": SCI_TEMP_BAND_C_MIN, "max": SCI_TEMP_BAND_C_MAX},
        "humidity_percent": (SCI_HUMIDITY_PCT_MIN + SCI_HUMIDITY_PCT_MAX) / 2.0,
        "humidity_band_pct": {"min": SCI_HUMIDITY_PCT_MIN, "max": SCI_HUMIDITY_PCT_MAX},
        "ambient_noise_db": None,
        "voc_index": None,
        "heart_rate_bpm": None,
        "ambient_light_lux": 5.0,
        "ambient_light_lux_band": {"min": SCI_LUX_MIN, "max": SCI_LUX_MAX},
    },
    "last_transition": None,
    "recent_changes": [],
}
monitor_thread_started = False
readiness_thread_started = False
optimal_band_thread_started = False
_last_optimal_band_utc_date = None


def to_float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def session_mean_heart_rate_bpm(open_session_pk):
    """Mean HR across the active sleep-session window (excluding invalid samples)."""
    if open_session_pk is None:
        return None
    sess = db.session.get(SleepSession, open_session_pk)
    if sess is None or sess.started_at is None:
        return None
    rows = reading.read_sleep_session_between(
        sess.started_at,
        get_current_utc_time(),
    )
    vals = []
    for row in rows:
        h = to_float_or_none(row.heart_rate)
        if h is not None and 35.0 <= float(h) <= 180.0:
            vals.append(float(h))
    if not vals:
        return None
    return sum(vals) / len(vals)


def readings_context_anonymized_for_llm(days=7, limit=2500, llm_budget_rows=1700):
    """
    Rolling window readings for Sleep Coach prompts — excludes primary keys and identifiers.
    """
    since = get_current_utc_time() - timedelta(days=days)
    rows = (
        Reading.query.filter(Reading.timestamp >= since)
        .order_by(Reading.timestamp.asc())
        .limit(limit)
        .all()
    )
    if len(rows) > llm_budget_rows:
        stride = max(1, len(rows) // llm_budget_rows)
        rows = rows[::stride][:llm_budget_rows]
    readings = []
    for r in rows:
        readings.append(
            {
                "timestamp": utc_isoformat_z(to_utc_datetime(r.timestamp)),
                "temperature_c": to_float_or_none(r.temperature),
                "humidity_pct": to_float_or_none(r.humidity),
                "air_quality_index": to_float_or_none(r.air_quality),
                "ambient_noise_db": to_float_or_none(r.ambient_noise),
                "ambient_light_lux": to_float_or_none(r.ambient_light),
                "heart_rate_bpm": to_float_or_none(r.heart_rate),
                "spo2_pct": to_float_or_none(r.spo2),
                "gyro_variance": to_float_or_none(r.gyro_variance),
            }
        )
    return {
        "window_days": days,
        "sample_rows_returned": len(readings),
        "notes": (
            "Anonymized time series for this deployment; "
            "no user_id or device identifiers included."
        ),
        "readings": readings,
    }


def build_sleep_night_points(readings):
    """
    Chronological Reading rows within a persisted sleep-session interval → JSON
    points including sequential PRV (ms), aligned with readiness logic.
    """
    points = []
    prev_rr = None
    for row in readings:
        hr = to_float_or_none(row.heart_rate)
        prv_ms = None
        if hr is not None and 35.0 <= hr <= 180.0:
            rr_ms = 60000.0 / hr
            if prev_rr is not None:
                prv_ms = round(abs(rr_ms - prev_rr), 2)
            prev_rr = rr_ms
        else:
            prev_rr = None

        points.append(
            {
                "t": utc_isoformat_z(to_utc_datetime(row.timestamp)),
                "heart_rate": hr,
                "prv_ms": prv_ms,
                "spo2": to_float_or_none(row.spo2),
                "gyro_variance": to_float_or_none(row.gyro_variance),
                "voc": to_float_or_none(row.air_quality),
                "ambient_noise": to_float_or_none(row.ambient_noise),
                "ambient_light": to_float_or_none(row.ambient_light),
            }
        )
    return points


def _wake_calendar_date(session_row):
    """UTC calendar day labeling a session: end date when closed, else start date."""
    if session_row.ended_at is not None:
        return to_utc_datetime(session_row.ended_at).date()
    return to_utc_datetime(session_row.started_at).date()


def _pick_completed_sleep_session_wake_date(target_d):
    """Most recently ended session whose wake day (UTC) matches ``target_d``."""
    best = None
    candidates = (
        SleepSession.query.filter(SleepSession.ended_at.isnot(None))
        .order_by(SleepSession.ended_at.desc())
        .limit(500)
        .all()
    )
    for s in candidates:
        if to_utc_datetime(s.ended_at).date() == target_d:
            if best is None or s.ended_at > best.ended_at:
                best = s
    return best


def _open_sleep_session_started_on(target_d):
    for s in (
        SleepSession.query.filter(SleepSession.ended_at.is_(None))
        .order_by(SleepSession.started_at.desc())
        .limit(30)
        .all()
    ):
        if to_utc_datetime(s.started_at).date() == target_d:
            return s
    return None


def resolve_sleep_session_for_night_chart(
    *,
    session_id_param=None,
    night_param=None,
):
    """
    Resolve which sleep session backs the Single Night charts.
    Rows span ``started_at`` through ``ended_at``, or ``now`` when still ASLEEP.
    Returns ``(session, error_message)`` for bad query params only.
    """
    if session_id_param is not None and str(session_id_param).strip():
        try:
            sid = int(session_id_param)
        except ValueError:
            return None, "Invalid session_id."
        sess = SleepSession.query.get(sid)
        return (sess if sess else None), None

    if night_param and str(night_param).strip():
        try:
            night_d = date.fromisoformat(str(night_param).strip())
        except ValueError:
            return None, "Invalid 'night'; use YYYY-MM-DD."
        cand = _pick_completed_sleep_session_wake_date(night_d)
        if cand is None:
            cand = _open_sleep_session_started_on(night_d)
        return cand, None

    consciousness, mem_sid = get_sleep_session_resolution_context()
    if consciousness == "ASLEEP" and mem_sid is not None:
        hit = SleepSession.query.get(mem_sid)
        if hit is not None:
            return hit, None

    open_sess = (
        SleepSession.query.filter(SleepSession.ended_at.is_(None))
        .order_by(SleepSession.started_at.desc())
        .first()
    )
    if open_sess is not None:
        return open_sess, None

    closed = (
        SleepSession.query.filter(SleepSession.ended_at.isnot(None))
        .order_by(SleepSession.ended_at.desc())
        .first()
    )
    return closed, None


def subjective_review_target_date(now_utc):
    """
    Wake-date (UTC) for the most recently completed sleep session we ask users
    to review as "last night".
    """
    if now_utc.hour >= 8:
        return now_utc.date()
    return now_utc.date() - timedelta(days=1)


def serialize_subjective_sleep_review(row):
    if row is None:
        return None
    return {
        "session_date": row.feedback_for_date.isoformat(),
        "rating": row.rating,
        "notes": row.notes,
        "saved_at": utc_isoformat_z(row.created_at),
    }


def serialize_subjective_sleep_review_history_entry(row):
    """Adds readiness from the closed sleep session ending on ``feedback_for_date`` (wake day UTC)."""
    base = serialize_subjective_sleep_review(row)
    sess = _latest_sleep_session_ended_on(row.feedback_for_date)
    if sess is not None and sess.readiness_score is not None:
        base["readiness_score"] = round(float(sess.readiness_score), 1)
    else:
        base["readiness_score"] = None
    return base


def compute_sunrise_sequence(now_utc, wake_time_str):
    sunrise_window_minutes = 30
    if not wake_time_str:
        wake_time_str = "07:00"

    try:
        wake_hour, wake_minute = wake_time_str.split(":")
        wake_hour = int(wake_hour)
        wake_minute = int(wake_minute)
    except (ValueError, TypeError):
        wake_hour, wake_minute = 7, 0

    wake_today = now_utc.replace(
        hour=wake_hour,
        minute=wake_minute,
        second=0,
        microsecond=0,
    )
    if now_utc > wake_today + timedelta(minutes=20):
        next_wake = wake_today + timedelta(days=1)
    else:
        next_wake = wake_today

    minutes_to_wake = (next_wake - now_utc).total_seconds() / 60.0
    raw_progress = (
        sunrise_window_minutes - max(minutes_to_wake, 0)
    ) / sunrise_window_minutes
    progress = min(max(raw_progress, 0.0), 1.0)
    active = 0.0 < progress <= 1.0

    lumen = int(40 + (760 * progress))
    color_temp_k = int(2200 + (2800 * progress))
    brightness_percent = int(5 + (95 * progress))
    phase = "pre_sunrise"
    if active:
        phase = "sunrise_ramp"
    elif progress >= 1.0:
        phase = "wake_now"

    return {
        "active": active,
        "phase": phase,
        "wake_time": f"{wake_hour:02d}:{wake_minute:02d}",
        "minutes_to_wake": round(minutes_to_wake, 1),
        # UTC instant for client: (next_wake_unix_ms - Date.now()) / 60000 — no local TZ math
        "next_wake_unix_ms": int(next_wake.timestamp() * 1000),
        "progress_percent": round(progress * 100, 1),
        "lumen": lumen,
        "color_temperature_k": color_temp_k,
        "brightness_percent": brightness_percent,
    }


def append_room_change(event_type, reason, payload):
    event = {
        "timestamp": utc_isoformat_z(get_current_utc_time()),
        "event_type": event_type,
        "reason": reason,
        "payload": payload,
    }
    simulated_room_state["recent_changes"].append(event)
    simulated_room_state["recent_changes"] = simulated_room_state["recent_changes"][-20:]
    simulated_room_state["last_transition"] = event["timestamp"]


def compute_simulated_hardware(
    interventions_ok,
    drift_detected,
    dev_sim,
    *,
    voc_drift_active=False,
    biological_cooling=False,
):
    """
    Maps environmental drift (+ dev toggles + biological hints) to actuator badges.

    Drift-backed temperature/noise rules contribute only while asleep except:
    * VOC/air-quality excursions arm ventilation/filtration regardless of consciousness.
    * ``biological_cooling`` (Rule A) engages cooling while ASLEEP when live HR beats the
      session-average HR by >10% and room °C exceeds 19.0 °C.
    Dev toggles contribute regardless of consciousness.
    """
    out = {
        "cooling": False,
        "heater": False,
        "white_noise": False,
        "fan": False,
        "air_filtration_high_fan": False,
    }
    ds = dev_sim if isinstance(dev_sim, dict) else {}
    ft = ds.get("force_high_temperature") is True
    fn = ds.get("force_high_noise") is True
    fl = ds.get("force_low_temperature") is True
    fv = ds.get("force_voc_spike") is True

    if ft:
        out["cooling"] = True
        out["fan"] = True
    if fn:
        out["white_noise"] = True
        out["fan"] = True
    if fl:
        out["heater"] = True
    if fv:
        out["air_filtration_high_fan"] = True
        out["fan"] = True

    drift_set = set(drift_detected or [])
    if voc_drift_active:
        out["air_filtration_high_fan"] = True
        out["fan"] = True

    if interventions_ok:
        if "temperature" in drift_set:
            out["cooling"] = True
            out["fan"] = True
        if "noise" in drift_set:
            out["white_noise"] = True
            out["fan"] = True

    if biological_cooling:
        out["cooling"] = True
        out["fan"] = True

    return out


def refresh_simulated_hardware_locked():
    """Recompute simulated_hardware; must already hold ``sim_room_lock``."""
    dev = simulated_room_state.get("dev_simulation") or {}
    drift = simulated_room_state.get("last_drift_detected") or []
    interventions_ok = bool(simulated_room_state.get("sleep_mode_active"))
    hints = simulated_room_state.get("_autonomy_hints") or {}
    simulated_room_state["simulated_hardware"] = compute_simulated_hardware(
        interventions_ok,
        drift,
        dev,
        voc_drift_active=bool(hints.get("voc_drift_active")),
        biological_cooling=bool(hints.get("biological_cooling_high_hr_hot")),
    )


def calculate_sleep_readiness_worker():
    """
    Backfills readiness on closed sessions missing metrics (repair path).
    Normal path: finalize on gyro wake runs ``finalize_sleep_session_after_wake``.
    """
    while True:
        with app.app_context():
            pending = SleepSession.query.filter(
                SleepSession.ended_at.isnot(None),
                SleepSession.readiness_score.is_(None),
            ).all()
            for s in pending:
                compute_sleep_readiness_for_session(s.id)
        time.sleep(1800)


def update_optimal_band_worker():
    """Once per UTC day shortly after sleep window ends (~08:35+)."""
    global _last_optimal_band_utc_date
    while True:
        time.sleep(60)
        with app.app_context():
            now_utc = get_current_utc_time()
            today = now_utc.date()
            if now_utc.hour < 8 or (now_utc.hour == 8 and now_utc.minute < 35):
                continue
            if _last_optimal_band_utc_date == today:
                continue
            run_daily_optimal_band_updates()
            _last_optimal_band_utc_date = today


def evaluate_sleep_and_environment():
    while True:
        time.sleep(10)
        with app.app_context():
            latest = Reading.query.order_by(Reading.timestamp.desc()).first()
            if latest is None:
                continue

            evaluate_sleep_state(app)
            consciousness = get_user_sleep_consciousness_state()
            asleep = consciousness == "ASLEEP"
            sleep_snapshot = snapshot_sleep_tracking()
            sleep_signal_score_map = {"AWAKE": 0, "SETTLING": 2, "ASLEEP": 5}
            sleep_signal_score = sleep_signal_score_map.get(consciousness, 0)

            temp = to_float_or_none(latest.temperature)
            humid = to_float_or_none(latest.humidity)
            noise = to_float_or_none(latest.ambient_noise)
            voc = to_float_or_none(latest.air_quality)
            hr = to_float_or_none(latest.heart_rate)

            # Prefer first account's persisted optimal °F band as sleep HVAC target (°C).
            ref_user = User.query.order_by(User.id.asc()).first()
            if ref_user is not None:
                _lo = float(ref_user.cfg_optimal_band_f_min or 65.0)
                _hi = float(ref_user.cfg_optimal_band_f_max or 68.0)
                _mid_f = (_lo + _hi) / 2.0
                sleep_target_c = round((_mid_f - 32.0) * (5.0 / 9.0), 2)
            else:
                sleep_target_c = 19.5
            drift_cool_margin = 0.5

            with sim_room_lock:
                dev_locked = simulated_room_state.get("dev_simulation") or {}
                temp_display = temp
                voc_display = voc
                if dev_locked.get("force_low_temperature") is True:
                    if ref_user is not None:
                        g_min_f = float(ref_user.cfg_guardrail_temp_f_min or 60.0)
                        g_min_c = (g_min_f - 32.0) * (5.0 / 9.0)
                        temp_display = round(g_min_c - 2.0, 2)
                    else:
                        temp_display = 12.0
                if dev_locked.get("force_voc_spike") is True:
                    base_voc = float(voc) if voc is not None else 0.0
                    voc_display = max(base_voc, 950.0)

                simulated_room_state["environment"] = {
                    "temperature_c": temp_display,
                    "humidity_percent": humid,
                    "ambient_noise_db": noise,
                    "voc_index": voc_display,
                }

                simulated_room_state["sleep_tracking"] = dict(sleep_snapshot)
                simulated_room_state["current_user_state"] = consciousness
                simulated_room_state["sleep_mode_active"] = asleep
                simulated_room_state["sleep_onset_confirmed"] = asleep

                simulated_room_state["sleep_signal_score"] = sleep_signal_score

                baselines = simulated_room_state["baselines"]
                if baselines.get("ambient_noise_db") is None and noise is not None:
                    baselines["ambient_noise_db"] = noise
                if baselines.get("voc_index") is None and voc is not None:
                    baselines["voc_index"] = voc

                if hr is not None and 35.0 <= float(hr) <= 180.0:
                    bh = baselines.get("heart_rate_bpm")
                    if bh is None:
                        baselines["heart_rate_bpm"] = float(hr)
                    else:
                        baselines["heart_rate_bpm"] = round(
                            0.88 * float(bh) + 0.12 * float(hr),
                            2,
                        )

                drift_detected = []
                temp_base = baselines["temperature_c"]
                hum_base = baselines["humidity_percent"]
                noise_base = baselines["ambient_noise_db"]
                voc_base = baselines["voc_index"]
                if (
                    temp is not None and temp_base is not None
                    and abs(temp - temp_base) > 1.5
                ):
                    drift_detected.append("temperature")
                if (
                    humid is not None and hum_base is not None
                    and abs(humid - hum_base) > 7.0
                ):
                    drift_detected.append("humidity")
                if (
                    noise is not None and noise_base is not None
                    and noise > noise_base + 8.0
                ):
                    drift_detected.append("noise")

                # VOC / MQ‑135: autonomous hazard corridor — evaluated outside ASLEEP gating.
                voc_abs_alarm = (
                    voc is not None and float(voc) > MQ135_SAFE_INDEX_MAX
                )
                voc_rel_alarm = (
                    voc is not None
                    and voc_base is not None
                    and float(voc_base) > 0
                    and float(voc) > float(voc_base) * 1.25
                )
                voc_alarm_active = voc_abs_alarm or voc_rel_alarm

                session_pk = sleep_snapshot.get("sleep_session_id")
                session_hr_mean = session_mean_heart_rate_bpm(
                    session_pk if asleep else None,
                )
                # Rule A: elevated HR vs *session* average + warm room (>19 °C) → biological cooling hint.
                corr_rule_a_high_hr_hot = (
                    asleep
                    and hr is not None
                    and session_hr_mean is not None
                    and float(session_hr_mean) > 0
                    and float(hr) > float(session_hr_mean) * 1.10
                    and temp is not None
                    and float(temp) > 19.0
                )
                simulated_room_state["_autonomy_hints"] = {
                    "voc_drift_active": voc_alarm_active,
                    "biological_cooling_high_hr_hot": corr_rule_a_high_hr_hot,
                }

                ma_ctx = simulated_room_state.setdefault(
                    "_micro_arousal_ctx",
                    default_micro_arousal_ctx(),
                )
                micro_evt = micro_arousal_tick(
                    ma_ctx,
                    now_utc=get_current_utc_time(),
                    heart_rate_bpm=hr,
                    ambient_noise_db=noise,
                    noise_baseline_db=noise_base,
                )
                if micro_evt is not None:
                    db.session.add(micro_evt)
                    db.session.commit()
                    append_room_change(
                        "micro_arousal",
                        micro_evt.disruption_label,
                        {
                            "spike_noise_db": micro_evt.spike_noise_db,
                            "prv_median_before_ms": micro_evt.prv_median_before_ms,
                            "prv_observed_ms": micro_evt.prv_observed_ms,
                            "prv_drop_ms": micro_evt.prv_drop_ms,
                        },
                    )

                interventions_ok = asleep

                prev_voc_evt = simulated_room_state.get("_voc_boost_event_prev", False)
                if voc_alarm_active and not prev_voc_evt:
                    append_room_change(
                        "voc_ventilation_boost",
                        (
                            "VOC hazard (MQ‑135) — broadcasting high-fan ventilation "
                            "(independent of sleep state)"
                        ),
                        {
                            "voc_live": voc,
                            "voc_baseline": voc_base,
                            "mq135_safe_ceiling": MQ135_SAFE_INDEX_MAX,
                            "absolute_trigger": voc_abs_alarm,
                            "relative_trigger": voc_rel_alarm,
                        },
                    )
                simulated_room_state["_voc_boost_event_prev"] = voc_alarm_active

                if interventions_ok:
                    simulated_room_state["state"] = "consciousness_sleep"
                    simulated_room_state["occupied"] = True
                    simulated_room_state["lighting"] = {
                        "state": "dimmed",
                        "brightness_percent": 6,
                        "lumens_nominal": 2,
                        "lumens_range_lm": {"min": 0, "max": 10},
                    }
                    base_hvac_sleep = {
                        "mode": "cooling",
                        "target_temperature_c": sleep_target_c,
                    }
                    simulated_room_state["hvac"] = dict(base_hvac_sleep)
                    if drift_detected:
                        simulated_room_state["ventilation"] = {
                            "mode": "boost",
                            "fan_percent": 80,
                        }
                        if "temperature" in drift_detected:
                            simulated_room_state["hvac"] = {
                                "mode": "cooling",
                                "target_temperature_c": round(
                                    sleep_target_c - drift_cool_margin, 2,
                                ),
                            }
                        append_room_change(
                            "environmental_drift",
                            "baseline drift interventions (armed only during ASLEEP)",
                            {
                                "drift_metrics": drift_detected,
                                "consciousness_state": consciousness,
                                "current_environment": simulated_room_state[
                                    "environment"
                                ],
                            },
                        )
                    else:
                        simulated_room_state["ventilation"] = {
                            "mode": "normal",
                            "fan_percent": 25,
                        }

                else:
                    simulated_room_state["state"] = "occupied_monitoring"
                    simulated_room_state["occupied"] = True
                    simulated_room_state["lighting"] = {
                        "state": "ambient",
                        "brightness_percent": 8,
                        "lumens_nominal": 5,
                        "lumens_range_lm": {"min": 0, "max": 10},
                    }
                    simulated_room_state["hvac"] = {
                        "mode": "circulating",
                        "target_temperature_c": 17.0,
                    }
                    simulated_room_state["ventilation"] = {
                        "mode": "normal",
                        "fan_percent": 25,
                    }

                # VOC response is independent of sleep state; always wins over drift defaults.
                if voc_alarm_active:
                    simulated_room_state["ventilation"] = {
                        "mode": "boost",
                        "fan_percent": 95,
                        "reason": "voc_air_quality_exception",
                    }

                simulated_room_state["last_drift_detected"] = list(drift_detected)
                refresh_simulated_hardware_locked()


def start_background_tasks():
    global monitor_thread_started, readiness_thread_started, optimal_band_thread_started
    if not monitor_thread_started:
        worker = threading.Thread(
            target=evaluate_sleep_and_environment,
            name="sleep-environment-monitor",
            daemon=True,
        )
        worker.start()
        monitor_thread_started = True

    if not readiness_thread_started:
        readiness_worker = threading.Thread(
            target=calculate_sleep_readiness_worker,
            name="sleep-readiness-calculator",
            daemon=True,
        )
        readiness_worker.start()
        readiness_thread_started = True

    if not optimal_band_thread_started:
        band_worker = threading.Thread(
            target=update_optimal_band_worker,
            name="optimal-band-daily-update",
            daemon=True,
        )
        band_worker.start()
        optimal_band_thread_started = True


def _digest_ingest_key(value: str) -> bytes:
    """Fixed-length SHA-256 digest for timing-safe ingest key comparison."""
    return hashlib.sha256((value or "").encode("utf-8")).digest()


# waits for the ESP32 hardware to send a JSON payload.
@app.route('/post-data', methods=['POST'])
def receive_data():
    global total_records_cache
    expected_key = (os.getenv("INGEST_API_KEY") or "").strip()
    if not expected_key:
        return {
            "error": "Server misconfiguration: INGEST_API_KEY is not set",
        }, 503

    supplied = (request.headers.get("X-API-KEY") or "").strip()
    if not secrets.compare_digest(
        _digest_ingest_key(supplied),
        _digest_ingest_key(expected_key),
    ):
        return {"error": "Unauthorized"}, 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "JSON object body required"}, 400

    try:
        # Hand the payload to Pydantic (ReadingBase) for cleaning and validation
        clean_data = ReadingBase(**payload)

        reading.create(
            temperature=ingest_field_plaintext(clean_data.temperature),
            humidity=ingest_field_plaintext(clean_data.humidity),
            air_quality=ingest_field_plaintext(clean_data.air_quality),
            ambient_noise=ingest_field_plaintext(clean_data.ambient_noise),
            ambient_light=ingest_field_plaintext(clean_data.ambient_light),
            heart_rate=ingest_field_plaintext(clean_data.heart_rate),
            spo2=ingest_field_plaintext(clean_data.spo2),
            gyro_variance=ingest_field_plaintext(clean_data.gyro_variance),
        )

        try:
            evaluate_sleep_state(app)
        except Exception:
            app.logger.exception("evaluate_sleep_state failed after ingest")

        if total_records_cache is not None:
            total_records_cache += 1

        # Reply to the ESP32 to let it know the data was received and saved
        return {"status": "success"}, 200
    except ValidationError as e:
        return {"error": "Invalid data format", "details": e.errors()}, 400
    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/api/user-config', methods=['GET', 'POST'])
@login_required
def user_config():
    allowed_wake_days = {"daily", "weekdays", "weekends"}

    if request.method == 'GET':
        return {
            "temp_min": current_user.cfg_temp_min,
            "temp_max": current_user.cfg_temp_max,
            "noise_limit": current_user.cfg_noise_limit,
            "wake_time": current_user.cfg_wake_time,
            "wake_days": current_user.cfg_wake_days or "daily",
            "guardrail_temp_f_min": (
                current_user.cfg_guardrail_temp_f_min
                if current_user.cfg_guardrail_temp_f_min is not None
                else 60.0
            ),
            "guardrail_temp_f_max": (
                current_user.cfg_guardrail_temp_f_max
                if current_user.cfg_guardrail_temp_f_max is not None
                else 75.0
            ),
            "optimal_band_f_min": (
                current_user.cfg_optimal_band_f_min
                if current_user.cfg_optimal_band_f_min is not None
                else 65.0
            ),
            "optimal_band_f_max": (
                current_user.cfg_optimal_band_f_max
                if current_user.cfg_optimal_band_f_max is not None
                else 68.0
            ),
            "override_optimal_band": bool(current_user.cfg_override_optimal_band),
        }, 200

    payload = request.get_json(silent=True) or {}

    def parse_optional_float(value):
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("numeric fields must be valid numbers")

    guardrail_f_min = guardrail_f_max = optimal_f_min = optimal_f_max = None
    try:
        if "temp_min" in payload:
            current_user.cfg_temp_min = parse_optional_float(
                payload.get("temp_min")
            )
        if "temp_max" in payload:
            current_user.cfg_temp_max = parse_optional_float(
                payload.get("temp_max")
            )
        if "noise_limit" in payload:
            current_user.cfg_noise_limit = parse_optional_float(
                payload.get("noise_limit")
            )

        if "wake_time" in payload:
            wake_val = payload.get("wake_time")
            if wake_val in ("", None):
                current_user.cfg_wake_time = None
            else:
                if (
                    not isinstance(wake_val, str)
                    or len(wake_val) != 5
                    or wake_val[2] != ":"
                ):
                    return {"error": "wake_time must be in HH:MM format"}, 400
                hh, mm = wake_val.split(":")
                if not (hh.isdigit() and mm.isdigit()):
                    return {"error": "wake_time must be in HH:MM format"}, 400
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    return {"error": "wake_time must be in HH:MM format"}, 400
                current_user.cfg_wake_time = wake_val

        if "wake_days" in payload:
            wd = payload.get("wake_days") or "daily"
            if wd not in allowed_wake_days:
                return {
                    "error": "wake_days must be daily, weekdays, or weekends",
                }, 400
            current_user.cfg_wake_days = wd

        has_guardrail_min = "guardrail_temp_f_min" in payload
        has_guardrail_max = "guardrail_temp_f_max" in payload
        if has_guardrail_min:
            guardrail_f_min = parse_optional_float(
                payload.get("guardrail_temp_f_min")
            )
            if guardrail_f_min is None:
                return {"error": "guardrail_temp_f_min invalid"}, 400
            current_user.cfg_guardrail_temp_f_min = guardrail_f_min
        if has_guardrail_max:
            guardrail_f_max = parse_optional_float(
                payload.get("guardrail_temp_f_max")
            )
            if guardrail_f_max is None:
                return {"error": "guardrail_temp_f_max invalid"}, 400
            current_user.cfg_guardrail_temp_f_max = guardrail_f_max

        eff_g_min = (
            current_user.cfg_guardrail_temp_f_min or 60.0
        )
        eff_g_max = (
            current_user.cfg_guardrail_temp_f_max or 75.0
        )
        if eff_g_min >= eff_g_max:
            return {"error": "guardrail_temp_f_min must be below max"}, 400
        if eff_g_min < 40 or eff_g_max > 110:
            return {
                "error": "guardrail range must be within 40°F and 110°F",
            }, 400

        has_ob_min = "optimal_band_f_min" in payload
        has_ob_max = "optimal_band_f_max" in payload
        if has_ob_min != has_ob_max:
            return {
                "error": "send both optimal_band_f_min and optimal_band_f_max",
            }, 400
        if has_ob_min:
            optimal_f_min = parse_optional_float(
                payload.get("optimal_band_f_min")
            )
            optimal_f_max = parse_optional_float(
                payload.get("optimal_band_f_max")
            )
            if optimal_f_min is None or optimal_f_max is None:
                return {"error": "optimal band values invalid"}, 400
            if optimal_f_min >= optimal_f_max:
                return {"error": "optimal_band_f_min must be below max"}, 400
            if optimal_f_min < eff_g_min or optimal_f_max > eff_g_max:
                return {
                    "error": "optimal band must lie within absolute guardrails",
                }, 400
            current_user.cfg_optimal_band_f_min = optimal_f_min
            current_user.cfg_optimal_band_f_max = optimal_f_max

        if "override_optimal_band" in payload:
            override_optimal = payload.get("override_optimal_band")
            if not isinstance(override_optimal, bool):
                return {
                    "error": "override_optimal_band must be boolean",
                }, 400
            current_user.cfg_override_optimal_band = override_optimal
    except ValueError as err:
        return {"error": str(err)}, 400
    db.session.commit()

    return {
        "status": "saved",
        "config": {
            "temp_min": current_user.cfg_temp_min,
            "temp_max": current_user.cfg_temp_max,
            "noise_limit": current_user.cfg_noise_limit,
            "wake_time": current_user.cfg_wake_time,
            "wake_days": current_user.cfg_wake_days,
            "guardrail_temp_f_min": (
                current_user.cfg_guardrail_temp_f_min or 60.0
            ),
            "guardrail_temp_f_max": (
                current_user.cfg_guardrail_temp_f_max or 75.0
            ),
            "optimal_band_f_min": (
                current_user.cfg_optimal_band_f_min or 65.0
            ),
            "optimal_band_f_max": (
                current_user.cfg_optimal_band_f_max or 68.0
            ),
            "override_optimal_band": bool(
                current_user.cfg_override_optimal_band
            ),
        },
    }, 200


@app.route('/api/subjective-sleep-review/status', methods=['GET'])
@login_required
def subjective_sleep_review_status():
    now_utc = get_current_utc_time()
    target = subjective_review_target_date(now_utc)
    row = SubjectiveSleepReview.query.filter_by(
        user_id=current_user.id,
        feedback_for_date=target,
    ).first()
    return {
        "target_session_date": target.isoformat(),
        "has_review": row is not None,
        "review": serialize_subjective_sleep_review(row),
    }, 200


@app.route('/api/subjective-sleep-review', methods=['POST'])
@login_required
def subjective_sleep_review_save():
    try:
        payload = SubjectiveSleepReviewIn.model_validate(
            request.get_json(silent=True) or {}
        )
    except ValidationError as exc:
        return {"error": "Invalid request body", "details": exc.errors()}, 400

    now_utc = get_current_utc_time()
    session_date = payload.session_date or subjective_review_target_date(now_utc)
    notes_str = (
        payload.notes.strip()
        if payload.notes is not None and payload.notes.strip()
        else None
    )

    row = SubjectiveSleepReview.query.filter_by(
        user_id=current_user.id,
        feedback_for_date=session_date,
    ).first()
    if row:
        row.rating = payload.rating
        row.notes = notes_str
        row.created_at = get_current_utc_time()
    else:
        db.session.add(
            SubjectiveSleepReview(
                user_id=current_user.id,
                feedback_for_date=session_date,
                rating=payload.rating,
                notes=notes_str,
            )
        )

    db.session.commit()
    saved = SubjectiveSleepReview.query.filter_by(
        user_id=current_user.id,
        feedback_for_date=session_date,
    ).first()
    return {
        "status": "saved",
        "review": serialize_subjective_sleep_review(saved),
    }, 200


@app.route('/api/subjective-sleep-review/history', methods=['GET'])
@login_required
def subjective_sleep_review_history():
    limit_raw = request.args.get("limit", "40")
    try:
        limit = int(limit_raw)
    except ValueError:
        return {"error": "limit must be a positive integer"}, 400
    if limit <= 0:
        return {"error": "limit must be a positive integer"}, 400
    limit = min(limit, 120)

    days_raw = request.args.get("days")
    start_cutoff = None
    if days_raw is not None and str(days_raw).strip() != "":
        try:
            days_win = int(days_raw)
        except ValueError:
            return {"error": "days must be a positive integer"}, 400
        if days_win < 1 or days_win > 90:
            return {"error": "days must be between 1 and 90"}, 400
        start_cutoff = get_current_utc_time().date() - timedelta(days=days_win - 1)

    q = SubjectiveSleepReview.query.filter_by(user_id=current_user.id)
    if start_cutoff is not None:
        q = q.filter(SubjectiveSleepReview.feedback_for_date >= start_cutoff)

    rows = (
        q.order_by(SubjectiveSleepReview.feedback_for_date.desc())
        .limit(limit)
        .all()
    )
    return {
        "days_window": days_raw,
        "count": len(rows),
        "reviews": [serialize_subjective_sleep_review_history_entry(r) for r in rows],
    }, 200


# called by JavaScript on the dashboard every 30 seconds to get the newest reading without refreshing the page.
@app.route('/api/latest-readings')
def latest_readings():
    # updates the "Current Readings" box on the dashboard
    latest = Reading.query.order_by(Reading.timestamp.desc()).first()
    if latest:
        return {
            "temperature": latest.temperature,
            "humidity": latest.humidity,
            "air_quality": latest.air_quality,
            "ambient_noise": latest.ambient_noise,
            "ambient_light": latest.ambient_light,
            "heart_rate": latest.heart_rate,
            "spo2": latest.spo2,
            "gyro_variance": latest.gyro_variance,
            "timestamp": utc_isoformat_z(latest.timestamp),
        }
    return {
        "temperature": "N/A",
        "humidity": "N/A",
        "air_quality": "N/A",
        "ambient_noise": "N/A",
        "ambient_light": "N/A",
        "heart_rate": "N/A",
        "spo2": "N/A",
        "gyro_variance": "N/A",
        "timestamp": None,
    }, 200


@app.route("/api/sleep-coach", methods=["POST"])
@login_required
def api_sleep_coach():
    payload = request.get_json(silent=True) or {}
    user_q = (
        payload.get("query")
        or payload.get("q")
        or payload.get("message")
        or ""
    )
    user_q = str(user_q).strip()
    if not user_q:
        msg = (
            'JSON body must include text, e.g. {"query": "How can I sleep better?"}'
        )
        return {"error": msg}, 400

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return {"error": "OPENAI_API_KEY is not set"}, 503

    anonymized_bundle = readings_context_anonymized_for_llm(days=7, limit=2500)
    coach_model = (
        (os.getenv("OPENAI_SLEEP_COACH_MODEL") or "gpt-4o-mini").strip()
        or "gpt-4o-mini"
    )
    sys_prompt = (
        "You are the user's premium sleep coach: calm, authoritative, and brief—similar in "
        "tone to the Oura app. You answer using only the anonymized bedroom IoT time series "
        "in the user message below (timestamps and sensor fields: temperature, humidity, air "
        "quality, noise, light, HR, SpO₂, motion/gyro). There are no PII strings. Ground "
        "advice in what those signals imply plus sound sleep physiology.\n\n"
        "Formatting (required): Respond in strict, highly scannable Markdown. Prefer short "
        "bullet lists (one idea per bullet). Use `##` section headers when helpful, each "
        "with a concise title and a fitting emoji—for example 🌬️ Air quality, 🫀 Vitals, "
        "💡 Environment (pick emojis that match the section).\n\n"
        "Tone and content rules:\n"
        "- Never open with robotic meta-phrases such as \"Based on the provided readings\" "
        "or \"Based on the data\"; start like a coach speaking to the user.\n"
        "- Do not apologize, hedge about gaps, or mention missing/absent/unavailable sensors "
        "or values. Silently analyze only what appears in the JSON; skip topics with no usable "
        "values.\n"
        "- Give practical sleep-hygiene and environment tweaks aligned with the data.\n"
        "- Do not invent medical diagnoses or replace a clinician; if something is "
        "concerning, state it succinctly as a reason to seek professional advice."
    )
    serialized_ctx = json.dumps(anonymized_bundle)
    max_ctx_chars = min(340_000, int(os.getenv("SLEEP_COACH_CONTEXT_CHARS", "340000")))
    if len(serialized_ctx) > max_ctx_chars:
        serialized_ctx = serialized_ctx[:max_ctx_chars] + "…truncated-for-token-cap"

    user_block = (
        "Anonymized readings JSON (possibly truncated):\n"
        f"{serialized_ctx}\n\n"
        f"User question:\n{user_q}"
    )

    try:
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=coach_model,
            temperature=0.45,
            max_tokens=900,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_block},
            ],
        )
        recommendation = (
            completion.choices[0].message.content or ""
        ).strip()
        if not recommendation:
            return {"error": "Model returned empty text"}, 502
    except Exception as exc:
        return {"error": f"Upstream LLM failure: {exc}"}, 502

    return {
        "recommendation": recommendation,
        "model": coach_model,
        "samples_in_context": anonymized_bundle["sample_rows_returned"],
    }


def _serialize_sleep_session_score(row):
    if row is None:
        return None
    wake_day = (
        to_utc_datetime(row.ended_at).date().isoformat()
        if row.ended_at is not None
        else to_utc_datetime(row.started_at).date().isoformat()
    )
    return {
        "id": row.id,
        "score_date": wake_day,
        "readiness_score": row.readiness_score,
        "sleep_efficiency_pct": row.sleep_efficiency_pct,
        "sleep_efficiency_score": row.sleep_efficiency_score,
        "avg_prv_ms": row.avg_prv_ms,
        "prv_score": row.prv_score,
        "spo2_drop_count": row.spo2_drop_count,
        "noise_spike_count": row.noise_spike_count,
        "disturbance_score": row.disturbance_score,
        "sample_count": row.sample_count,
        "calculated_at": (
            utc_isoformat_z(row.calculated_at)
            if row.calculated_at is not None else None
        ),
    }


@app.route('/api/sleep-readiness/latest')
@login_required
def sleep_readiness_latest():
    latest_score = (
        SleepSession.query.filter(
            SleepSession.ended_at.isnot(None),
            SleepSession.readiness_score.isnot(None),
        )
        .order_by(SleepSession.ended_at.desc())
        .first()
    )
    if not latest_score:
        return {"score": None}, 200
    return {"score": _serialize_sleep_session_score(latest_score)}, 200


@app.route('/api/sleep-readiness/history')
@login_required
def sleep_readiness_history():
    limit_raw = request.args.get("limit", "14")
    try:
        limit = int(limit_raw)
    except ValueError:
        return {"error": "limit must be a positive integer"}, 400
    if limit <= 0:
        return {"error": "limit must be a positive integer"}, 400
    limit = min(limit, 90)

    rows = (
        SleepSession.query.filter(
            SleepSession.ended_at.isnot(None),
            SleepSession.readiness_score.isnot(None),
        )
        .order_by(SleepSession.ended_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "count": len(rows),
        "scores": [_serialize_sleep_session_score(r) for r in rows],
    }, 200


@app.route('/api/sleep-session/list')
@login_required
def sleep_session_list():
    """Recent FSM-derived sleep sessions for the Single Night picker."""
    consciousness, active_sid = get_sleep_session_resolution_context()
    rows = (
        SleepSession.query.order_by(SleepSession.started_at.desc())
        .limit(50)
        .all()
    )
    items = []
    for s in rows:
        ongoing = s.ended_at is None
        items.append({
            "id": s.id,
            "wake_date_utc": _wake_calendar_date(s).isoformat(),
            "started_at": utc_isoformat_z(s.started_at),
            "ended_at": utc_isoformat_z(s.ended_at) if s.ended_at else None,
            "ongoing": ongoing,
            "fsm_active": bool(
                ongoing
                and active_sid is not None
                and s.id == active_sid
                and consciousness == "ASLEEP"
            ),
        })
    return {"sessions": items}, 200


@app.route('/api/sleep-session/night-readings')
@login_required
def sleep_session_night_readings():
    sess, param_err = resolve_sleep_session_for_night_chart(
        session_id_param=request.args.get("session_id"),
        night_param=request.args.get("night"),
    )
    if param_err:
        return {"error": param_err}, 400
    if sess is None:
        return {"error": "No sleep sessions on record.", "session_id": None}, 404

    window_start = to_utc_datetime(sess.started_at)
    window_end = sess.ended_at if sess.ended_at else get_current_utc_time()

    readings = reading.read_sleep_session_between(window_start, window_end)
    points = build_sleep_night_points(readings)

    label_date = _wake_calendar_date(sess).isoformat()

    return {
        "session_id": sess.id,
        "night": label_date,
        "session_start_time": utc_isoformat_z(window_start),
        "session_end_time": utc_isoformat_z(window_end),
        "ongoing": sess.ended_at is None,
        "window_start": utc_isoformat_z(window_start),
        "window_end": utc_isoformat_z(window_end),
        "sample_count": len(points),
        "points": points,
    }, 200


@app.route('/api/sleep-readiness/weekly-summary')
@login_required
def sleep_readiness_weekly_summary():
    rows_chrono = (
        SleepSession.query.filter(
            SleepSession.ended_at.isnot(None),
            SleepSession.readiness_score.isnot(None),
        )
        .order_by(SleepSession.ended_at.desc())
        .limit(7)
        .all()
    )
    rows_chrono = list(reversed(rows_chrono))

    days_out = []
    for hit in rows_chrono:
        wd = to_utc_datetime(hit.ended_at).date().isoformat()
        days_out.append({
            "score_date": wd,
            "session_id": hit.id,
            "readiness_score": round(float(hit.readiness_score), 2),
            "avg_prv_ms": (
                round(float(hit.avg_prv_ms), 2)
                if hit.avg_prv_ms is not None else None
            ),
        })

    if rows_chrono:
        range_start = to_utc_datetime(rows_chrono[0].ended_at).date().isoformat()
        range_end = (
            to_utc_datetime(rows_chrono[-1].ended_at).date().isoformat()
        )
    else:
        rd = get_current_utc_time().date().isoformat()
        range_start = rd
        range_end = rd

    return {
        "range_start": range_start,
        "range_end": range_end,
        "days": days_out,
    }, 200


# called by JavaScript on the dashboard every 30 seconds to get the newest reading without refreshing the page.
@app.route('/api/system-status')
@login_required
def system_status():
    global total_records_cache

    start_time = time.perf_counter()
    # updates the 'System Diagnostics' boxes on the Info page.
    latest = Reading.query.order_by(Reading.timestamp.desc()).first()

    # use raw SQL here to pull the physical encrypted string out of the database without python auto-decrypting it first.
    raw_row = db.session.execute(db.text("SELECT temperature, humidity FROM readings ORDER BY id DESC LIMIT 1")).fetchone()

    if total_records_cache is None:
        total_records_cache = Reading.query.count()

    efficiency_ms = (time.perf_counter() - start_time) * 1000

    if not latest or not raw_row:
        return {
            "temp": "N/A", "hum": "N/A",
            "raw_t": "N/A", "raw_h": "N/A",
            "count": total_records_cache,
            "efficiency": f"{efficiency_ms:.2f}ms",  # Add to JSON
            "status": "WAITING FOR DATA...",
            "timestamp": None
        }, 200

    now = get_current_utc_time()
    last_time = to_utc_datetime(latest.timestamp)

    time_since_last = now - last_time

    # If the reading is older than 30 seconds, consider the stream dead
    if time_since_last.total_seconds() > 30:
        return {
            "temp": "--",
            "hum": "--",
            "raw_t": "--",
            "raw_h": "--",
            "count": total_records_cache,
            "efficiency": f"{efficiency_ms:.2f}ms",
            "status": "CONNECTION LOST / WAITING...",
            # Keep sending the timestamp so the UI shows exactly when it died
            "timestamp": utc_isoformat_z(latest.timestamp)
        }, 200

    is_encrypted = str(raw_row[0]) != str(latest.temperature)
    current_status = "AES ENCRYPTED PAYLOAD DETECTED" if is_encrypted else "PLAINTEXT DATA DETECTED"

    return {
        "temp": latest.temperature,
        "hum": latest.humidity,
        "raw_t": raw_row[0],
        "raw_h": raw_row[1],
        "count": total_records_cache,
        "efficiency": f"{efficiency_ms:.2f}ms", # Add to JSON
        "status": current_status,
        "timestamp": utc_isoformat_z(latest.timestamp)
    }, 200


@app.route('/api/simulated-room', methods=['GET', 'POST'])
def simulated_room():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        allowed_keys = {
            "cooling",
            "heater",
            "white_noise",
            "fan",
            "air_filtration_high_fan",
        }
        incoming_keys = set(payload.keys())
        invalid_keys = sorted(incoming_keys - allowed_keys)
        if invalid_keys:
            return {
                "error": "Invalid hardware keys",
                "invalid_keys": invalid_keys,
                "allowed_keys": sorted(list(allowed_keys)),
            }, 400

        with sim_room_lock:
            hardware = simulated_room_state.get("simulated_hardware", {})
            updates = {}
            for key in allowed_keys:
                if key in payload:
                    raw_value = payload[key]
                    if not isinstance(raw_value, bool):
                        return {
                            "error": f"'{key}' must be boolean"
                        }, 400
                    hardware[key] = raw_value
                    updates[key] = raw_value
            simulated_room_state["simulated_hardware"] = hardware
            if updates:
                append_room_change(
                    "simulated_hardware_update",
                    "manual state update via API",
                    updates,
                )

            response_payload = dict(simulated_room_state)
            response_payload["timestamp"] = utc_isoformat_z(
                get_current_utc_time()
            )
            response_payload["recent_changes"] = list(
                simulated_room_state["recent_changes"]
            )
        return response_payload, 200

    wake_time = None
    if current_user.is_authenticated:
        wake_time = current_user.cfg_wake_time
    now_utc = get_current_utc_time()
    sunrise = compute_sunrise_sequence(now_utc, wake_time)

    with sim_room_lock:
        response_payload = dict(simulated_room_state)
        lighting_state = dict(simulated_room_state.get("lighting", {}))
        lighting_state["brightness_percent"] = sunrise["brightness_percent"]
        lighting_state["lumen"] = sunrise["lumen"]
        lighting_state["color_temperature_k"] = sunrise["color_temperature_k"]
        response_payload["lighting"] = lighting_state
        response_payload["sunrise_sequence"] = sunrise
        response_payload["timestamp"] = utc_isoformat_z(now_utc)
        response_payload["recent_changes"] = list(
            simulated_room_state["recent_changes"]
        )
    return response_payload, 200


@app.route('/api/dev/simulation', methods=['POST'])
@login_required
def dev_simulation_toggle():
    """
    Dashboard-only presentation toggles. Forces intervention badges ACTIVE without real sensor drift.

    POST JSON may include booleans ``force_high_temperature``, ``force_high_noise``,
    ``force_low_temperature``, and ``force_voc_spike``.
    Omitted keys are left unchanged (partial updates supported).
    """
    if getattr(current_user, "role", None) != "Admin":
        return {"error": "Administrator role required"}, 403

    payload = request.get_json(silent=True) or {}
    allowed_flags = frozenset(
        {
            "force_high_temperature",
            "force_high_noise",
            "force_low_temperature",
            "force_voc_spike",
        }
    )
    incoming = set(payload.keys())
    unsupported = sorted(incoming - allowed_flags)
    if unsupported:
        return {
            "error": "Unknown keys",
            "unknown_keys": unsupported,
            "allowed_keys": sorted(allowed_flags),
        }, 400

    with sim_room_lock:
        dev = simulated_room_state.setdefault(
            "dev_simulation",
            {
                "force_high_temperature": False,
                "force_high_noise": False,
                "force_low_temperature": False,
                "force_voc_spike": False,
            },
        )
        for key in allowed_flags:
            if key not in payload:
                continue
            val = payload[key]
            if not isinstance(val, bool):
                return {"error": f"'{key}' must be a boolean"}, 400
            dev[key] = val

        refresh_simulated_hardware_locked()
        out = {
            "dev_simulation": dict(dev),
            "simulated_hardware": dict(
                simulated_room_state.get("simulated_hardware", {})
            ),
            "timestamp": utc_isoformat_z(get_current_utc_time()),
        }
    return out, 200


@app.route('/api/simulated-room/changes')
def simulated_room_changes():
    since_param = request.args.get("since")
    cursor_param = request.args.get("cursor")
    limit_param = request.args.get("limit")
    since_dt = None
    start_index = 0
    limit = 10

    if since_param:
        normalized_since = since_param.replace("Z", "+00:00")
        try:
            since_dt = datetime.fromisoformat(normalized_since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            else:
                since_dt = since_dt.astimezone(timezone.utc)
        except ValueError:
            return {
                "error": "Invalid 'since' format. Use ISO-8601 datetime."
            }, 400

    if cursor_param:
        try:
            start_index = int(cursor_param)
            if start_index < 0:
                raise ValueError
        except ValueError:
            return {
                "error": "Invalid 'cursor'. Use a non-negative integer."
            }, 400

    if limit_param:
        try:
            limit = int(limit_param)
            if limit <= 0:
                raise ValueError
            limit = min(limit, 50)
        except ValueError:
            return {
                "error": "Invalid 'limit'. Use a positive integer."
            }, 400

    with sim_room_lock:
        all_changes = list(simulated_room_state["recent_changes"])

    if since_dt is None:
        filtered_changes = all_changes
    else:
        filtered_changes = []
        for event in all_changes:
            event_ts = event.get("timestamp")
            if not event_ts:
                continue
            try:
                event_dt = datetime.fromisoformat(
                    event_ts.replace("Z", "+00:00")
                )
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
                else:
                    event_dt = event_dt.astimezone(timezone.utc)
            except ValueError:
                continue

            if event_dt > since_dt:
                filtered_changes.append(event)

    paged_changes = filtered_changes[start_index:start_index + limit]
    next_index = start_index + len(paged_changes)
    has_more = next_index < len(filtered_changes)

    return {
        "count": len(paged_changes),
        "total_matching": len(filtered_changes),
        "cursor": str(start_index),
        "next_cursor": str(next_index) if has_more else None,
        "has_more": has_more,
        "changes": paged_changes,
    }, 200


# A memory dictionary. Save weather result here with timestamp. If same city
# requested again within 10 minutes, serve saved copy instead of hitting API again.
weather_cache = {}
CACHE_TIMEOUT = 600  # 10 minutes (in seconds)


@app.route('/api/weather')
def get_weather():
    # default to Kansas City if no city provided
    city = request.args.get('city', 'Kansas City')  
    current_time = time.time()

    # Check if we have asked the API about this city recently.
    if city in weather_cache:
        cached_data = weather_cache[city]
        # If the saved copy is less than 10 minutes old, serve that copy
        if current_time - cached_data['timestamp'] < CACHE_TIMEOUT:
            return cached_data['data']

    # If no saved copy, get the secret API key from .env file
    api_key = os.getenv("WEATHER_API_KEY")
    if not api_key:
        return {"error": "API key missing. Check .env file."}, 500

    # Build URL and send GET request out to internet using 'requests' library.
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={city}"
    try:
        response = requests.get(url)
        data = response.json()

        # If the weather API responded with a success, save a copy in our cache for the next 10 minutes.
        if "error" not in data:
            weather_cache[city] = {
                'data': data,
                'timestamp': current_time
            }
        return data
    except Exception as e:
        return {"error": str(e)}, 500


@app.route('/')
def home():
    # This automatically sends the user to the login page if they just type the IP address
    return redirect(url_for('login'))


if __name__ == "__main__":
    start_background_tasks()
    # Ad hoc TLS (requires PyOpenSSL). Access https://<host>:8888 — browser will warn on self-signed cert.
    app.run(host='0.0.0.0', port=8888, ssl_context='adhoc')
