"""Sleep charts, subjective review serialization, and Sleep Coach context (used by API routes)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from config import (
    SUBJECTIVE_DISCREPANCY_MAX_STARS,
    SUBJECTIVE_DISCREPANCY_MIN_ALGORITHM_READINESS,
)
from crud import reading as reading_crud
from db import db
from logic import _latest_sleep_session_ended_on, get_sleep_session_resolution_context
from schemas.reading import Reading
from schemas.sleep_score_discrepancy_log import SleepScoreDiscrepancyLog
from schemas.sleep_session import SleepSession
from schemas.subjective_sleep_review import SubjectiveSleepReview
from sleep_metrics import READINESS_SCORE_FORMULA_VERSION
from timefmt import to_utc_datetime, utc_isoformat_z
from utils import (
    format_restlessness_band_from_score,
    get_current_utc_time,
    restlessness_score_from_raw,
)


def to_float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def readings_context_anonymized_for_llm(
    *,
    days: int = 7,
    limit: int = 2500,
    llm_budget_rows: int = 1700,
    user_id: int,
) -> Dict[str, Any]:
    """Rolling-window readings for Sleep Coach prompts — no PII columns."""
    since = get_current_utc_time() - timedelta(days=days)
    q = (
        Reading.query.filter(Reading.timestamp >= since)
        .filter(Reading.user_id == user_id)
        .order_by(Reading.timestamp.asc())
        .limit(limit)
    )
    rows = q.all()
    if len(rows) > llm_budget_rows:
        stride = max(1, len(rows) // llm_budget_rows)
        rows = rows[::stride][:llm_budget_rows]
    readings = []
    for r in rows:
        point = {
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
        # Compact context: remove missing keys and round numeric payload.
        compact = {}
        for k, v in point.items():
            if v is None:
                continue
            if isinstance(v, float):
                compact[k] = round(v, 2)
            else:
                compact[k] = v
        readings.append(compact)
    return {
        "window_days": days,
        "sample_rows_returned": len(readings),
        "notes": (
            "Anonymized time series for this deployment; "
            "no user_id or device identifiers included."
        ),
        "readings": readings,
    }


def build_sleep_night_points(readings: List[Reading]) -> List[Dict[str, Any]]:
    """Chronological points for a sleep window (PRV aligned with readiness logic)."""
    points: List[Dict[str, Any]] = []
    prev_rr: Optional[float] = None
    for row in readings:
        hr = to_float_or_none(row.heart_rate)
        prv_ms: Optional[float] = None
        if hr is not None and 35.0 <= hr <= 180.0:
            rr_ms = 60000.0 / hr
            if prev_rr is not None:
                prv_ms = round(abs(rr_ms - prev_rr), 2)
            prev_rr = rr_ms
        else:
            prev_rr = None

        rs = restlessness_score_from_raw(row.gyro_variance)
        temp_c = to_float_or_none(row.temperature)
        room_temp_f = (
            round(temp_c * (9.0 / 5.0) + 32.0, 2)
            if temp_c is not None
            else None
        )
        pt: Dict[str, Any] = {
            "t": utc_isoformat_z(to_utc_datetime(row.timestamp)),
            "heart_rate": hr,
            "prv_ms": prv_ms,
            "spo2": to_float_or_none(row.spo2),
            "gyro_variance": to_float_or_none(row.gyro_variance),
            "restlessness_score": rs,
            "voc": to_float_or_none(row.air_quality),
            "ambient_noise": to_float_or_none(row.ambient_noise),
            "ambient_light": to_float_or_none(row.ambient_light),
            "room_temp_f": room_temp_f,
            "humidity": to_float_or_none(row.humidity),
        }
        if rs is not None:
            band = format_restlessness_band_from_score(rs)
            if band:
                pt["restlessness"] = band
        points.append(pt)
    return points


def _wake_calendar_date(session_row: SleepSession) -> date:
    """UTC calendar day for session labelling: wake day when closed, else start day."""
    if session_row.ended_at is not None:
        return to_utc_datetime(session_row.ended_at).date()
    return to_utc_datetime(session_row.started_at).date()


def _format_sleep_session_display_label(s: SleepSession) -> str:
    wake_d = _wake_calendar_date(s)
    wk = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[wake_d.weekday()]
    mon = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )[wake_d.month - 1]
    return f"{wk} {wake_d.day} {mon} wake"


def _pick_completed_sleep_session_wake_date(
    target_d: date, user_id: int
) -> Optional[SleepSession]:
    best: Optional[SleepSession] = None
    candidates = (
        SleepSession.query.filter(
            SleepSession.ended_at.isnot(None),
            SleepSession.user_id == user_id,
        )
        .order_by(SleepSession.ended_at.desc())
        .limit(500)
        .all()
    )
    for s in candidates:
        if s.ended_at is not None and to_utc_datetime(s.ended_at).date() == target_d:
            if best is None or s.ended_at > best.ended_at:
                best = s
    return best


def _open_sleep_session_started_on(target_d: date, user_id: int) -> Optional[SleepSession]:
    for s in (
        SleepSession.query.filter(
            SleepSession.ended_at.is_(None),
            SleepSession.user_id == user_id,
        )
        .order_by(SleepSession.started_at.desc())
        .limit(30)
        .all()
    ):
        if to_utc_datetime(s.started_at).date() == target_d:
            return s
    return None


def resolve_sleep_session_for_night_chart(
    *,
    session_id_param: Any = None,
    night_param: Any = None,
    viewer_user_id: int,
) -> tuple[Optional[SleepSession], Optional[str]]:
    if session_id_param is not None and str(session_id_param).strip():
        try:
            sid = int(session_id_param)
        except ValueError:
            return None, "Invalid session_id."
        sess = db.session.get(SleepSession, sid)
        if sess is None or getattr(sess, "user_id", None) != viewer_user_id:
            return None, "Session not found."
        return sess, None

    if night_param and str(night_param).strip():
        try:
            night_d = date.fromisoformat(str(night_param).strip())
        except ValueError:
            return None, "Invalid 'night'; use YYYY-MM-DD."
        cand = _pick_completed_sleep_session_wake_date(night_d, viewer_user_id)
        if cand is None:
            cand = _open_sleep_session_started_on(night_d, viewer_user_id)
        return cand, None

    consciousness, mem_sid = get_sleep_session_resolution_context()
    if consciousness == "ASLEEP" and mem_sid is not None:
        hit = db.session.get(SleepSession, mem_sid)
        if hit is not None and getattr(hit, "user_id", None) == viewer_user_id:
            return hit, None

    open_sess = (
        SleepSession.query.filter(
            SleepSession.user_id == viewer_user_id,
            SleepSession.ended_at.is_(None),
        )
        .order_by(SleepSession.started_at.desc())
        .first()
    )
    if open_sess is not None:
        return open_sess, None

    closed = (
        SleepSession.query.filter(
            SleepSession.user_id == viewer_user_id,
            SleepSession.ended_at.isnot(None),
        )
        .order_by(SleepSession.ended_at.desc())
        .first()
    )
    return closed, None


def subjective_review_target_date(now_utc: datetime) -> date:
    """Wake-date (UTC) used as default for 'last night' subjective review."""
    if now_utc.hour >= 8:
        return now_utc.date()
    return now_utc.date() - timedelta(days=1)


def serialize_subjective_sleep_review(row: Optional[SubjectiveSleepReview]) -> Any:
    if row is None:
        return None
    return {
        "session_date": row.feedback_for_date.isoformat(),
        "rating": row.rating,
        "notes": row.notes,
        "saved_at": utc_isoformat_z(row.created_at),
    }


def serialize_subjective_sleep_review_history_entry(
    row: SubjectiveSleepReview,
) -> Dict[str, Any]:
    base = serialize_subjective_sleep_review(row)
    sess = _latest_sleep_session_ended_on(row.feedback_for_date, row.user_id)
    if sess is not None and sess.readiness_score is not None:
        base["readiness_score"] = round(float(sess.readiness_score), 1)
    else:
        base["readiness_score"] = None
    return base


def _mean(nums: List[float]) -> Optional[float]:
    return round(sum(nums) / len(nums), 3) if nums else None


def _session_sensor_nightly_averages(sess: SleepSession) -> Dict[str, Optional[float]]:
    end = sess.ended_at if sess.ended_at is not None else get_current_utc_time()
    uid = getattr(sess, "user_id", None)
    rows = reading_crud.read_sleep_session_between(
        to_utc_datetime(sess.started_at),
        to_utc_datetime(end),
        user_id=uid,
    )
    hrs: List[float] = []
    spo2s: List[float] = []
    hrvs: List[float] = []
    lux: List[float] = []
    voc: List[float] = []
    noise: List[float] = []
    rest: List[float] = []
    for r in rows:
        h = to_float_or_none(r.heart_rate)
        if h is not None and 35.0 <= h <= 180.0:
            hrs.append(h)
        s = to_float_or_none(r.spo2)
        if s is not None:
            spo2s.append(s)
        hv = getattr(r, "hrv_rmssd", None)
        hvf = to_float_or_none(hv)
        if hvf is not None:
            hrvs.append(hvf)
        lx = to_float_or_none(r.ambient_light)
        if lx is not None:
            lux.append(lx)
        aq = to_float_or_none(r.air_quality)
        if aq is not None:
            voc.append(aq)
        nd = to_float_or_none(r.ambient_noise)
        if nd is not None:
            noise.append(nd)
        g = to_float_or_none(r.gyro_variance)
        if g is not None:
            rs = restlessness_score_from_raw(g)
            if rs is not None:
                rest.append(float(rs))
    return {
        "avg_heart_rate_bpm": _mean(hrs),
        "avg_spo2_pct": _mean(spo2s),
        "avg_hrv_rmssd_ms": _mean(hrvs),
        "avg_ambient_light_lux": _mean(lux),
        "avg_air_quality_index": _mean(voc),
        "avg_ambient_noise_db": _mean(noise),
        "avg_restlessness_score": _mean(rest),
    }


def _bind_subjective_review_to_ground_truth(
    row: SubjectiveSleepReview,
    user_id: int,
    session_date: date,
    rating: int,
) -> bool:
    """Link review to closed session; log large subjective-vs-algorithm gaps."""
    sess = _latest_sleep_session_ended_on(session_date, user_id)
    discrepancy_logged = False
    if sess is None:
        row.linked_sleep_session_id = None
        row.algorithm_readiness_snapshot = None
        return False

    row.linked_sleep_session_id = sess.id
    ar: Optional[float] = None
    if sess.readiness_score is not None:
        ar = float(sess.readiness_score)
        row.algorithm_readiness_snapshot = ar

    if (
        ar is not None
        and rating <= SUBJECTIVE_DISCREPANCY_MAX_STARS
        and ar >= SUBJECTIVE_DISCREPANCY_MIN_ALGORITHM_READINESS
    ):
        exists = SleepScoreDiscrepancyLog.query.filter_by(
            user_id=user_id,
            feedback_for_date=session_date,
        ).first()
        if exists is None:
            db.session.add(
                SleepScoreDiscrepancyLog(
                    user_id=user_id,
                    feedback_for_date=session_date,
                    sleep_session_id=sess.id,
                    stars=rating,
                    algorithm_readiness=ar,
                    formula_version=READINESS_SCORE_FORMULA_VERSION,
                )
            )
            discrepancy_logged = True
    return discrepancy_logged
