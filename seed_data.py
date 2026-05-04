#!/usr/bin/env python3
"""
Development-only SQLite seeder: high-frequency simulated ESP32-style readings.

Generates exactly **3 consecutive nights** of data, **only between 22:00 and
07:00 the next calendar day in America/Chicago (US Central)** (9 hours per night),
one reading every
**15 seconds** (~2160 rows per night). Uses three distinct profiles (great /
poor / average) so readiness scores differ across nights.

Clears ``readings`` and ``sleep_sessions``, inserts matching ``SleepSession``
rows and encrypted reading rows (same AES-GCM as the Flask app). Requires
``MASTER_ENCRYPTION_KEY`` in ``.env``.

Usage (from project root)::

    python seed_data.py
    python seed_data.py --database instance/server.db
    python seed_data.py --seed 99
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from flask import Flask

from db import db
from schemas.reading import Reading
from schemas.sleep_session import SleepSession
from schemas.user import User
from sleep_metrics import compute_sleep_readiness_for_session
from utils import encrypt_at_rest

STEP_SECONDS = 15
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 7
# Simulated “bed” window uses wall clock in US Central (handles CST/CDT).
NIGHT_TZ = ZoneInfo("America/Chicago")


def _store_as_utc(dt: datetime) -> datetime:
    """
    Persist instants as timezone-aware UTC.

    SQLite often returns naive values; the Flask app treats naive DB times as UTC
    (``to_utc_datetime``). Chicago wall-clock samples must be converted here so
    22:00–07:00 Central is not misread as 22:00–07:00 UTC on the dashboard.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ensure_user_scope_columns() -> None:
    """SQLite: add ``user_id`` if missing (standalone seed without starting Flask first)."""
    rcols = {
        row[1]
        for row in db.session.execute(db.text("PRAGMA table_info(readings)")).fetchall()
    }
    if "user_id" not in rcols:
        db.session.execute(db.text("ALTER TABLE readings ADD COLUMN user_id INTEGER"))
        db.session.commit()
    scols = {
        row[1]
        for row in db.session.execute(db.text("PRAGMA table_info(sleep_sessions)")).fetchall()
    }
    if "user_id" not in scols:
        db.session.execute(db.text("ALTER TABLE sleep_sessions ADD COLUMN user_id INTEGER"))
        db.session.commit()


def _resolve_sqlite_uri(explicit: Optional[str]) -> str:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        return "sqlite:///" + str(p).replace("\\", "/")
    primary = ROOT / "server.db"
    inst = ROOT / "instance" / "server.db"
    if primary.exists():
        path = primary
    elif inst.exists():
        path = inst
    else:
        path = primary
    return "sqlite:///" + str(path.resolve()).replace("\\", "/")


def _sleep_window_for_wake_date(wake_d: date) -> Tuple[datetime, datetime]:
    """Night interval: previous Chicago calendar day 22:00 -> wake_d 07:00 Chicago."""
    tz = NIGHT_TZ
    start = datetime.combine(
        wake_d - timedelta(days=1),
        time(NIGHT_START_HOUR, 0, 0),
        tzinfo=tz,
    )
    end = datetime.combine(
        wake_d,
        time(NIGHT_END_HOUR, 0, 0),
        tzinfo=tz,
    )
    return start, end


def _three_wake_dates_central() -> Tuple[date, date, date]:
    """Three consecutive completed wake mornings (07:00 Chicago), all in the past."""
    now = datetime.now(NIGHT_TZ)
    today = now.date()
    if now.hour < NIGHT_END_HOUR or (
        now.hour == NIGHT_END_HOUR and now.minute == 0 and now.second == 0
    ):
        last_wake = today - timedelta(days=1)
    else:
        last_wake = today
    w3 = last_wake
    w2 = last_wake - timedelta(days=1)
    w1 = last_wake - timedelta(days=2)
    return w1, w2, w3


def _encrypt_field(val: Any) -> Optional[str]:
    if val is None:
        return None
    return encrypt_at_rest(str(val))


def _row_mapping(
    ts: datetime,
    *,
    user_id: int,
    temperature: float,
    humidity: float,
    air_quality: float,
    ambient_noise: float,
    ambient_light: float,
    heart_rate: float,
    spo2: float,
    gyro_variance: float,
    hrv_rmssd: float,
) -> Dict[str, Any]:
    """Keys match ``Reading`` mapped column attributes for ``bulk_insert_mappings``."""
    spo2_c = min(100.0, max(0.0, spo2))
    return {
        "timestamp": ts,
        "user_id": user_id,
        "_temperature": _encrypt_field(f"{temperature:.2f}"),
        "_humidity": _encrypt_field(f"{humidity:.1f}"),
        "_air_quality": _encrypt_field(f"{air_quality:.0f}"),
        "_ambient_noise": _encrypt_field(f"{ambient_noise:.1f}"),
        "_ambient_light": _encrypt_field(f"{ambient_light:.2f}"),
        "_heart_rate": _encrypt_field(f"{heart_rate:.1f}"),
        "_spo2": _encrypt_field(f"{spo2_c:.1f}"),
        "_gyro_variance": _encrypt_field(f"{gyro_variance:.4f}"),
        "_hrv_rmssd": _encrypt_field(f"{hrv_rmssd:.1f}"),
    }


def _profile_great(phase: float, rng: random.Random, tick: int) -> Dict[str, float]:
    """Night 1: restorative — low HR, high SpO2, very still, cool room."""
    hr = 55.0 + 2.8 * math.sin(phase * math.pi) + rng.gauss(0, 0.55)
    hr = max(51.0, min(62.0, hr))
    spo2 = min(100.0, 97.8 + rng.uniform(0, 1.6))
    gyro = 0.035 + abs(rng.gauss(0, 0.018))
    if rng.random() < 0.006:
        gyro = min(0.18, gyro + rng.uniform(0.06, 0.12))
    gyro = max(0.02, min(0.22, gyro))
    temp_c = 18.85 + 0.12 * math.sin(phase * math.pi * 2) + rng.uniform(-0.06, 0.06)
    humidity = 49.0 + rng.uniform(-1.8, 1.8)
    voc = 215.0 + rng.uniform(-18, 22)
    noise = 29.0 + rng.uniform(-1.2, 1.4)
    lux = abs(rng.gauss(0, 0.35)) if rng.random() < 0.94 else rng.uniform(0.4, 1.8)
    hrv = 42.0 + 10.0 * (1.0 - phase) * 0.45 + rng.uniform(-3.5, 4.5)
    hrv = max(22.0, min(62.0, hrv))
    return {
        "temperature": temp_c,
        "humidity": humidity,
        "air_quality": max(80.0, voc),
        "ambient_noise": max(22.0, noise),
        "ambient_light": max(0.0, lux),
        "heart_rate": hr,
        "spo2": spo2,
        "gyro_variance": gyro,
        "hrv_rmssd": hrv,
    }


def _profile_poor(phase: float, rng: random.Random, tick: int) -> Dict[str, float]:
    """Night 2: fragmented — warmer, louder, higher HR, frequent movement."""
    hr = 72.0 + 10.0 * math.sin(phase * math.pi * 1.1) + rng.gauss(0, 1.8)
    hr = max(64.0, min(92.0, hr))
    spo2 = min(100.0, 94.5 + rng.uniform(0, 2.8))
    if rng.random() < 0.12:
        spo2 = max(90.0, spo2 - rng.uniform(0.5, 2.0))
    gyro = 0.09 + abs(rng.gauss(0, 0.04))
    if rng.random() < 0.22:
        gyro = min(0.38, gyro + rng.uniform(0.1, 0.22))
    elif rng.random() < 0.08:
        gyro = min(0.32, gyro + rng.uniform(0.05, 0.15))
    temp_c = 20.4 + 0.55 * math.sin(phase * math.pi) + rng.uniform(-0.12, 0.12)
    humidity = 55.0 + rng.uniform(-2.0, 2.5)
    voc = 320.0 + rng.uniform(-25, 45)
    noise = 42.0 + rng.uniform(-3.0, 5.0)
    lux = abs(rng.gauss(0.2, 0.8)) if rng.random() < 0.88 else rng.uniform(1.0, 4.5)
    hrv = 24.0 + 6.0 * math.sin(phase * math.pi * 3) + rng.uniform(-5.0, 4.0)
    hrv = max(14.0, min(40.0, hrv))
    return {
        "temperature": temp_c,
        "humidity": humidity,
        "air_quality": max(120.0, voc),
        "ambient_noise": max(28.0, min(58.0, noise)),
        "ambient_light": max(0.0, lux),
        "heart_rate": hr,
        "spo2": spo2,
        "gyro_variance": max(0.02, gyro),
        "hrv_rmssd": hrv,
    }


def _profile_average(phase: float, rng: random.Random, tick: int) -> Dict[str, float]:
    """Night 3: typical — moderate vitals and occasional movement."""
    hr = 62.0 + 5.5 * math.sin(phase * math.pi * 0.95) + rng.gauss(0, 1.0)
    hr = max(56.0, min(76.0, hr))
    spo2 = min(100.0, 96.2 + rng.uniform(0, 2.0))
    gyro = 0.055 + abs(rng.gauss(0, 0.025))
    if rng.random() < 0.035:
        gyro = min(0.24, gyro + rng.uniform(0.07, 0.14))
    temp_c = 19.35 + 0.25 * math.sin(phase * math.pi * 2) + rng.uniform(-0.08, 0.08)
    humidity = 51.0 + rng.uniform(-2.0, 2.0)
    voc = 255.0 + rng.uniform(-22, 28)
    noise = 33.5 + rng.uniform(-2.0, 3.0)
    lux = abs(rng.gauss(0.15, 0.55)) if rng.random() < 0.9 else rng.uniform(0.3, 2.5)
    hrv = 32.0 + 8.0 * (1.0 - phase) * 0.35 + rng.uniform(-4.0, 5.0)
    hrv = max(18.0, min(52.0, hrv))
    return {
        "temperature": temp_c,
        "humidity": humidity,
        "air_quality": max(90.0, voc),
        "ambient_noise": max(25.0, min(48.0, noise)),
        "ambient_light": max(0.0, lux),
        "heart_rate": hr,
        "spo2": spo2,
        "gyro_variance": max(0.02, gyro),
        "hrv_rmssd": hrv,
    }


def _iter_ticks(window_start: datetime, window_end: datetime):
    """Yield (timestamp, phase_0_1) for each 15s sample in [start, end)."""
    span = (window_end - window_start).total_seconds()
    if span <= 0:
        return
    t = window_start
    while t < window_end:
        phase = (t - window_start).total_seconds() / span
        yield t, phase
        t += timedelta(seconds=STEP_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear readings/sleep_sessions and seed 3 nights of 15s ESP32-style data.",
    )
    parser.add_argument(
        "--database",
        type=str,
        default=None,
        help="SQLite path (default: server.db or instance/server.db).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20260202,
        help="RNG seed (default: 20260202).",
    )
    parser.add_argument(
        "--bulk-chunk",
        type=int,
        default=800,
        help="Rows per bulk_insert_mappings batch (default: 800).",
    )
    args = parser.parse_args()
    if args.bulk_chunk < 50:
        parser.error("--bulk-chunk must be >= 50")

    uri = _resolve_sqlite_uri(args.database)
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    w1, w2, w3 = _three_wake_dates_central()
    wake_dates = (w1, w2, w3)
    base_profiles: List[Callable[[float, random.Random, int], Dict[str, float]]] = [
        _profile_great,
        _profile_poor,
        _profile_average,
    ]

    with app.app_context():
        _ensure_user_scope_columns()
        db.session.execute(db.text("DELETE FROM readings"))
        db.session.execute(db.text("DELETE FROM sleep_sessions"))
        db.session.commit()
        print("Cleared tables: readings, sleep_sessions.")

        users = User.query.order_by(User.id.asc()).all()
        if not users:
            print("No users in database; log in once so admin/testuser exist, then re-run.")
            return 1

        session_ids: List[int] = []
        total_rows = 0
        chunk: List[Dict[str, Any]] = []

        for u in users:
            rot = u.id % 3
            profile_order = base_profiles[rot:] + base_profiles[:rot]
            rng_u = random.Random(args.seed + u.id * 9973)
            hr_bias = float((u.id * 3) % 11) - 5.0
            temp_bias = (((u.id * 5) % 9) - 4) * 0.04
            spo2_bias = float((u.id % 5)) * 0.12 - 0.24

            for idx in range(3):
                wake_d = wake_dates[idx]
                ws, we = _sleep_window_for_wake_date(wake_d)
                prof_fn = profile_order[idx]
                sess = SleepSession(
                    started_at=_store_as_utc(ws),
                    ended_at=_store_as_utc(we),
                    user_id=u.id,
                )
                db.session.add(sess)
                db.session.commit()
                session_ids.append(sess.id)
                print(
                    f"User {u.username!r}: SleepSession id={sess.id} "
                    f"(night {idx + 1}, profile={prof_fn.__name__}): "
                    f"Chicago wall {ws.strftime('%Y-%m-%d %H:%M')} -> {we.strftime('%Y-%m-%d %H:%M')} "
                    f"| stored {_store_as_utc(ws).isoformat()} -> {_store_as_utc(we).isoformat()}"
                )

                tick = 0
                for ts, phase in _iter_ticks(ws, we):
                    vals = prof_fn(phase, rng_u, tick)
                    vals["heart_rate"] = max(
                        35.0, min(180.0, vals["heart_rate"] + hr_bias)
                    )
                    vals["temperature"] = max(
                        10.0, min(32.0, vals["temperature"] + temp_bias)
                    )
                    vals["spo2"] = max(88.0, min(100.0, vals["spo2"] + spo2_bias))
                    chunk.append(
                        _row_mapping(
                            _store_as_utc(ts),
                            user_id=u.id,
                            temperature=vals["temperature"],
                            humidity=vals["humidity"],
                            air_quality=vals["air_quality"],
                            ambient_noise=vals["ambient_noise"],
                            ambient_light=vals["ambient_light"],
                            heart_rate=vals["heart_rate"],
                            spo2=vals["spo2"],
                            gyro_variance=vals["gyro_variance"],
                            hrv_rmssd=vals["hrv_rmssd"],
                        )
                    )
                    tick += 1
                    total_rows += 1
                    if len(chunk) >= args.bulk_chunk:
                        db.session.bulk_insert_mappings(Reading, chunk, render_nulls=True)
                        db.session.commit()
                        chunk.clear()

        if chunk:
            db.session.bulk_insert_mappings(Reading, chunk, render_nulls=True)
            db.session.commit()

        print(
            f"Bulk-inserted {total_rows} readings "
            f"(every {STEP_SECONDS}s, 22:00-07:00 America/Chicago wall -> UTC in DB, per user)."
        )

        for sid in session_ids:
            compute_sleep_readiness_for_session(sid)
            row = db.session.get(SleepSession, sid)
            if row and row.readiness_score is not None:
                print(
                    f"  Session {sid}: readiness_score={row.readiness_score} "
                    f"(samples={row.sample_count})"
                )

        ucount = User.query.count()
        print(f"Users table untouched ({ucount} user(s)).")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
