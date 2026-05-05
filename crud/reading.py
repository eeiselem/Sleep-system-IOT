from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import and_

from schemas.reading import Reading
from db import db


# DB helpers for reading rows

EVENT_LOG_ALLOWED_METRICS = frozenset(
    {
        "temperature",
        "humidity",
        "air_quality",
        "ambient_noise",
        "heart_rate",
        "spo2",
        "gyro_variance",
    }
)

EVENT_LOG_METRIC_LABELS = {
    "temperature": "Temperature (threshold °C; samples stored °C)",
    "humidity": "Humidity (%)",
    "air_quality": "Air quality index (device)",
    "ambient_noise": "Ambient noise (dB)",
    "heart_rate": "Heart rate (bpm)",
    "spo2": "SpO₂ (%)",
    "gyro_variance": (
        "Raw motion (gyro / activity; maps to restful efficiency in app)"
    ),
}


def _query_for_user(user_id=None):
    q = Reading.query
    if user_id is not None:
        q = q.filter(Reading.user_id == user_id)
    return q


def _reading_to_dict(reading: Reading):
    return {
        "timestamp": reading.timestamp,
        "temperature": reading.temperature,
        "humidity": reading.humidity,
        "air_quality": reading.air_quality,
        "ambient_noise": reading.ambient_noise,
        "ambient_light": reading.ambient_light,
        "heart_rate": reading.heart_rate,
        "spo2": reading.spo2,
        "gyro_variance": reading.gyro_variance,
        "hrv_rmssd": reading.hrv_rmssd,
    }


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def create(
    temperature=None,
    humidity=None,
    air_quality=None,
    ambient_noise=None,
    ambient_light=None,
    heart_rate=None,
    spo2=None,
    gyro_variance=None,
    hrv_rmssd=None,
    user_id=None,
):
    try:
        new_reading = Reading(
            temperature=temperature,
            humidity=humidity,
            air_quality=air_quality,
            ambient_noise=ambient_noise,
            ambient_light=ambient_light,
            heart_rate=heart_rate,
            spo2=spo2,
            gyro_variance=gyro_variance,
            hrv_rmssd=hrv_rmssd,
            user_id=user_id,
        )
        db.session.add(new_reading)
        db.session.commit()
        return new_reading

    except Exception as e:
        db.session.rollback()
        print("Error:", e)


def read_latest(user_id=None):
    try:
        q = _query_for_user(user_id)
        reading = q.order_by(
            Reading.timestamp.desc(),
            Reading.id.desc(),
        ).first()
        if reading is None:
            return None
        return _reading_to_dict(reading)
    except Exception as e:
        print("Error:", e)
        return None


def read_all(num_samples=50, user_id=None) -> List[Reading]:
    try:
        q = _query_for_user(user_id)
        readings = (
            q.order_by(Reading.timestamp.desc(), Reading.id.desc())
            .limit(num_samples)
            .all()
        )
        return readings
    except Exception as e:
        print("Error:", e)
        return None


def _parse_reading_numeric(value) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def read_event_log(
    *,
    metric: Optional[str] = None,
    threshold_raw: Optional[object] = None,
    direction: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    row_cap: int = 1200,
    user_id: Optional[int] = None,
) -> Optional[List[Reading]]:
    # Read event-log rows in time order, optional threshold filter.
    try:
        cap = max(1, min(int(row_cap), 5000))
    except (TypeError, ValueError):
        cap = 1200

    query = _query_for_user(user_id)

    end_filter = False
    if start_date:
        query = query.filter(Reading.timestamp >= start_date)
    if end_date:
        end_filter = True
        query = query.filter(Reading.timestamp <= end_date)

    if start_date is None and not end_filter:
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        query = query.filter(Reading.timestamp >= week_ago)

    readings = query.order_by(Reading.timestamp.asc()).limit(cap).all()

    metric_key = (metric or "").strip()
    threshold = None
    if threshold_raw not in (None, ""):
        try:
            threshold = float(threshold_raw)
        except (TypeError, ValueError):
            threshold = None

    direction_norm = direction if direction in ("above", "below") else None

    apply_metric = (
        metric_key in EVENT_LOG_ALLOWED_METRICS
        and threshold is not None
        and direction_norm is not None
    )

    if not apply_metric:
        return readings

    filtered = []
    for row in readings:
        raw_column = getattr(row, metric_key, None)
        value = _parse_reading_numeric(raw_column)
        if value is None:
            continue
        if direction_norm == "above" and value > threshold:
            filtered.append(row)
        elif direction_norm == "below" and value < threshold:
            filtered.append(row)

    return filtered


def read_sleep_session_between(
    session_start_utc: datetime,
    session_end_utc: datetime,
    user_id: Optional[int] = None,
) -> List[Reading]:
    # Read rows in one session window (UTC start/end).
    start = _as_utc(session_start_utc)
    end = _as_utc(session_end_utc)

    q = Reading.query.filter(
        and_(
            Reading.timestamp >= start,
            Reading.timestamp <= end,
        )
    )
    if user_id is not None:
        q = q.filter(Reading.user_id == user_id)
    return q.order_by(Reading.timestamp.asc(), Reading.id.asc()).all()


def read_search(
    *,
    metric: Optional[str] = None,
    threshold: Optional[object] = None,
    direction: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    threshold_temperature: Optional[object] = None,
    user_id: Optional[int] = None,
) -> Optional[List[Reading]]:
    # Backward-compatible wrapper around read_event_log.
    effective_metric = metric
    effective_thresh = threshold
    effective_dir = direction
    if (
        effective_metric in (None, "")
        and threshold_temperature not in (None, "")
    ):
        effective_metric = "temperature"
        effective_thresh = threshold_temperature
        effective_dir = effective_dir or "above"

    return read_event_log(
        metric=effective_metric,
        threshold_raw=effective_thresh,
        direction=effective_dir,
        start_date=start_date,
        end_date=end_date,
        user_id=user_id,
    )
