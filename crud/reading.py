from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import and_

from schemas.reading import Reading
from db import db


# functions for creating and reading data from the database

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
    "temperature": "Temperature (°C)",
    "humidity": "Humidity (%)",
    "air_quality": "Gas / VOC (index)",
    "ambient_noise": "Ambient noise (dB)",
    "heart_rate": "Heart rate (bpm)",
    "spo2": "SpO₂ (%)",
    "gyro_variance": "Gyro variance (movement)",
}


def create(
    temperature,
    humidity,
    air_quality=None,
    ambient_noise=None,
    ambient_light=None,
    heart_rate=None,
    spo2=None,
    gyro_variance=None,
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
        )
        db.session.add(new_reading)
        db.session.commit()
        return new_reading

    except Exception as e:
        db.session.rollback()
        print("Error:", e)


def read_latest():
    try:
        reading = Reading.query.order_by(Reading.timestamp.desc()).first()
        if reading is None:
            return None

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
        }
    except Exception as e:
        print("Error:", e)
        return None


def read_all(num_samples=50) -> List[Reading]:
    try:
        readings = (
            Reading.query
            .order_by(Reading.timestamp.desc())
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
) -> Optional[List[Reading]]:
    """
    Return readings ordered by ascending time within optional bounds.

    If ``metric``, ``threshold_raw``, and ``direction`` ('above' / 'below') are
    all valid, keep only rows whose decrypted numeric for that metric
    compares past the threshold.
    """
    try:
        cap = max(1, min(int(row_cap), 5000))
    except (TypeError, ValueError):
        cap = 1200

    query = Reading.query

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
) -> List[Reading]:
    """
    All readings with ``session_start_utc <= timestamp <= session_end_utc`` (UTC).

    Used for single-night charts and sleep readiness so bounds come only from the
    sleep onset state machine (ASLEEP → AWAKE), not fixed clock windows.
    """
    start = session_start_utc
    end = session_end_utc
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)

    return (
        Reading.query.filter(
            and_(
                Reading.timestamp >= start,
                Reading.timestamp <= end,
            )
        )
        .order_by(Reading.timestamp.asc())
        .all()
    )


def read_search(
    *,
    metric: Optional[str] = None,
    threshold: Optional[object] = None,
    direction: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    threshold_temperature: Optional[object] = None,
) -> Optional[List[Reading]]:
    """
    Compatibility wrapper: prefers ``metric`` / ``threshold``; falls back to
    legacy ``threshold_temperature`` (temperature °C column).
    """
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
    )
