"""Merged latest-reading snapshot for dashboard SSR and /api/latest-readings."""
from typing import Any, Dict, List, Optional

from schemas.reading import Reading
from timefmt import utc_isoformat_z
from utils import restlessness_score_from_raw


def _row_looks_biometric_only(r: Reading) -> bool:
    if r._air_quality is not None or r._ambient_noise is not None:
        return False
    if r._heart_rate is None and r._spo2 is None and getattr(r, "_hrv_rmssd", None) is None:
        return False
    try:
        t = (r.temperature or "").strip()
        h = (r.humidity or "").strip()
    except Exception:
        return False
    legacy_zeros = t in ("0", "0.0", "0.00") and h in ("0", "0.0", "0.00")
    null_th = r._temperature is None and r._humidity is None
    if not (legacy_zeros or null_th):
        return False
    return True


def _merge_latest_readings_display(rows: List[Reading]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    out: Dict[str, Any] = {}
    for r in rows:
        ts = utc_isoformat_z(r.timestamp)
        if r._heart_rate is not None and out.get("heart_rate") is None:
            out["heart_rate"] = r.heart_rate
            out["heart_rate_updated_at"] = ts
        if r._spo2 is not None and out.get("spo2") is None:
            out["spo2"] = r.spo2
            out["spo2_updated_at"] = ts
        if r._gyro_variance is not None and out.get("gyro_variance") is None:
            out["gyro_variance"] = r.gyro_variance
            out["gyro_variance_updated_at"] = ts
        if getattr(r, "_hrv_rmssd", None) is not None and out.get("hrv_rmssd") is None:
            out["hrv_rmssd"] = r.hrv_rmssd
            out["hrv_rmssd_updated_at"] = ts
    for r in rows:
        if _row_looks_biometric_only(r):
            continue
        ts = utc_isoformat_z(r.timestamp)
        if out.get("temperature") is None and r._temperature is not None:
            out["temperature"] = r.temperature
            out["temperature_updated_at"] = ts
        if out.get("humidity") is None and r._humidity is not None:
            out["humidity"] = r.humidity
            out["humidity_updated_at"] = ts
        if r._air_quality is not None and out.get("air_quality") is None:
            out["air_quality"] = r.air_quality
            out["air_quality_updated_at"] = ts
        if r._ambient_noise is not None and out.get("ambient_noise") is None:
            out["ambient_noise"] = r.ambient_noise
            out["ambient_noise_updated_at"] = ts
        if r._ambient_light is not None and out.get("ambient_light") is None:
            out["ambient_light"] = r.ambient_light
            out["ambient_light_updated_at"] = ts
    out["timestamp"] = utc_isoformat_z(rows[0].timestamp)
    return out


def _normalize_live_reading_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if not s or s.upper() == "N/A":
            return None
    return value


def build_latest_live_readings_payload(user_id: int) -> Dict[str, Any]:
    empty: Dict[str, Any] = {
        "timestamp": None,
        "temperature": None,
        "humidity": None,
        "air_quality": None,
        "ambient_noise": None,
        "ambient_light": None,
        "heart_rate": None,
        "spo2": None,
        "restlessness_score": None,
        "hrv_rmssd": None,
        "temperature_updated_at": None,
        "humidity_updated_at": None,
        "air_quality_updated_at": None,
        "ambient_noise_updated_at": None,
        "ambient_light_updated_at": None,
        "heart_rate_updated_at": None,
        "spo2_updated_at": None,
        "restlessness_score_updated_at": None,
        "hrv_rmssd_updated_at": None,
    }
    rows = (
        Reading.query.filter(Reading.user_id == user_id)
        .order_by(Reading.timestamp.desc(), Reading.id.desc())
        .limit(80)
        .all()
    )
    merged = _merge_latest_readings_display(rows)
    if not merged:
        return empty
    out = dict(empty)
    out["timestamp"] = merged.get("timestamp")
    for k in (
        "temperature",
        "humidity",
        "air_quality",
        "ambient_noise",
        "ambient_light",
        "heart_rate",
        "spo2",
        "hrv_rmssd",
    ):
        out[k] = _normalize_live_reading_scalar(merged.get(k))
    for k in (
        "temperature_updated_at",
        "humidity_updated_at",
        "air_quality_updated_at",
        "ambient_noise_updated_at",
        "ambient_light_updated_at",
        "heart_rate_updated_at",
        "spo2_updated_at",
        "hrv_rmssd_updated_at",
    ):
        out[k] = merged.get(k)
    gv_raw = _normalize_live_reading_scalar(merged.get("gyro_variance"))
    sc = restlessness_score_from_raw(gv_raw)
    if sc is not None:
        out["restlessness_score"] = float(sc)
    out["restlessness_score_updated_at"] = merged.get("gyro_variance_updated_at")
    return out
