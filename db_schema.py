"""SQLite schema patches and demo users (runs inside app context)."""
from sqlalchemy import text

from db import db
from extensions import bcrypt
from schemas.user import User


def ensure_reading_columns():
    existing_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(readings)")).fetchall()
    }
    required_columns = {
        "air_quality": "TEXT",
        "ambient_noise": "TEXT",
        "ambient_light": "TEXT",
        "heart_rate": "TEXT",
        "spo2": "TEXT",
        "gyro_variance": "TEXT",
        "hrv_rmssd": "TEXT",
        "user_id": "INTEGER",
    }

    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            db.session.execute(
                text(f"ALTER TABLE readings ADD COLUMN {column_name} {column_type}")
            )

    db.session.commit()


def ensure_readings_nullable_temperature_humidity():
    inf = db.session.execute(text("PRAGMA table_info(readings)")).fetchall()
    if not inf:
        return
    by_name = {row[1]: row for row in inf}
    t = by_name.get("temperature")
    h = by_name.get("humidity")
    if t is None or h is None:
        return
    if int(t[3]) == 0 and int(h[3]) == 0:
        return

    sorted_inf = sorted(inf, key=lambda r: r[0])
    col_parts = []
    for _cid, name, ctype, notnull, _dflt, pk in sorted_inf:
        sql_type = (ctype or "TEXT").upper()
        nn = int(notnull or 0)
        ipk = int(pk or 0)
        if name in ("temperature", "humidity"):
            nn = 0
        part = f'"{name}" {sql_type}'
        if name == "id":
            part += " PRIMARY KEY AUTOINCREMENT NOT NULL"
        elif ipk == 1:
            part += " NOT NULL PRIMARY KEY"
        elif nn == 1:
            part += " NOT NULL"
        col_parts.append(part)

    cols_order = [r[1] for r in sorted_inf]
    col_list_sql = ", ".join(f'"{c}"' for c in cols_order)
    tmp_name = "readings__nullable_th"
    db.session.execute(text(f"DROP TABLE IF EXISTS {tmp_name}"))

    db.session.execute(text(f"CREATE TABLE {tmp_name} ({', '.join(col_parts)})"))
    db.session.execute(
        text(f'INSERT INTO {tmp_name} ({col_list_sql}) SELECT {col_list_sql} FROM readings')
    )
    db.session.execute(text("DROP TABLE readings"))
    db.session.execute(text(f"ALTER TABLE {tmp_name} RENAME TO readings"))
    db.session.commit()


def ensure_sleep_session_columns():
    existing_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(sleep_sessions)")).fetchall()
    }
    if "user_id" not in existing_columns:
        db.session.execute(text("ALTER TABLE sleep_sessions ADD COLUMN user_id INTEGER"))
        db.session.commit()


def ensure_user_config_columns():
    existing_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(users)")).fetchall()
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
                text(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}")
            )

    db.session.commit()


def ensure_morning_sleep_feedback_ground_truth_columns():
    existing_columns = {
        row[1]
        for row in db.session.execute(text("PRAGMA table_info(morning_sleep_feedback)")).fetchall()
    }
    for column_name, column_type in (
        ("linked_sleep_session_id", "INTEGER"),
        ("algorithm_readiness_snapshot", "REAL"),
    ):
        if column_name not in existing_columns:
            db.session.execute(
                text(
                    "ALTER TABLE morning_sleep_feedback "
                    f"ADD COLUMN {column_name} {column_type}"
                )
            )
    db.session.commit()


def run_schema_patches():
    ensure_reading_columns()
    ensure_readings_nullable_temperature_humidity()
    ensure_sleep_session_columns()
    ensure_user_config_columns()
    ensure_morning_sleep_feedback_ground_truth_columns()


def ensure_demo_users():
    if not User.query.filter_by(username="admin").first():
        hashed_pw = bcrypt.generate_password_hash("admin").decode("utf-8")
        db.session.add(User(username="admin", password_hash=hashed_pw, role="Admin"))
        db.session.commit()
        print("--- Admin Account Created: Use 'admin' and 'admin' ---")

    if not User.query.filter_by(username="testuser").first():
        hashed_pw = bcrypt.generate_password_hash("password123").decode("utf-8")
        db.session.add(User(username="testuser", password_hash=hashed_pw, role="User"))
        db.session.commit()
