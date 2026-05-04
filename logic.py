"""Sleep / comfort tuning helpers (EMA optimal band updates)."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_

from db import db
from schemas.micro_arousal_event import MicroArousalEvent
from schemas.subjective_sleep_review import SubjectiveSleepReview
from schemas.reading import Reading
from schemas.sleep_session import SleepSession
from schemas.user import User
from sleep_metrics import finalize_sleep_session_after_wake
from utils import get_current_utc_time


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _utc_calendar_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).date()
    return dt.astimezone(timezone.utc).date()


def default_ingest_user_id() -> Optional[int]:
    """First user row (typical primary account) for unauthenticated device ingest."""
    u = User.query.order_by(User.id.asc()).first()
    return u.id if u else None


def _latest_sleep_session_ended_on(
    cal_date: date,
    scoped_user_id: Optional[int] = None,
) -> Optional[SleepSession]:
    q = SleepSession.query.filter(SleepSession.ended_at.isnot(None)).order_by(
        SleepSession.ended_at.desc(),
    )
    if scoped_user_id is not None:
        q = q.filter(SleepSession.user_id == scoped_user_id)
    candidates = q.all()
    for s in candidates:
        if s.ended_at is not None and _utc_calendar_date(s.ended_at) == cal_date:
            return s
    return None


def celsius_mean_between(
    window_start_utc: datetime,
    window_end_utc: datetime,
    user_id: Optional[int] = None,
) -> Optional[float]:
    """Mean room temperature over [start, end] UTC inclusive, converted to °F (same units as before)."""
    if window_start_utc.tzinfo is None:
        window_start_utc = window_start_utc.replace(tzinfo=timezone.utc)
    else:
        window_start_utc = window_start_utc.astimezone(timezone.utc)
    if window_end_utc.tzinfo is None:
        window_end_utc = window_end_utc.replace(tzinfo=timezone.utc)
    else:
        window_end_utc = window_end_utc.astimezone(timezone.utc)

    rq = (
        Reading.query.filter(Reading.timestamp >= window_start_utc)
        .filter(Reading.timestamp <= window_end_utc)
    )
    if user_id is not None:
        rq = rq.filter(Reading.user_id == user_id)
    readings = rq.all()
    vals = [_to_float(r.temperature) for r in readings]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    mean_c = sum(vals) / len(vals)
    return mean_c * 9.0 / 5.0 + 32.0


def alpha_from_sleep_readiness(readiness_score: float) -> float:
    """
    EMA blend factor: conservative for low readiness, strongest above 85.
    < 60 → no drift toward overnight mean; > 85 → alpha = 0.2.
    """
    if readiness_score < 60.0:
        return 0.0
    if readiness_score > 85.0:
        return 0.2
    return 0.2 * (readiness_score - 60.0) / (85.0 - 60.0)


def width_multiplier_from_morning_rating(rating: int) -> float:
    """
    Widens optimal band significantly for poor sleep reports (1–2),
    slightly narrows for good reports (4–5).
    """
    r = max(1, min(5, int(rating)))
    if r == 1:
        return 1.82
    if r == 2:
        return 1.45
    if r == 3:
        return 1.0
    if r == 4:
        return 0.92
    return 0.86


def _guardrails_f(user: User) -> Tuple[float, float]:
    gmin = (
        float(user.cfg_guardrail_temp_f_min)
        if user.cfg_guardrail_temp_f_min is not None
        else 60.0
    )
    gmax = (
        float(user.cfg_guardrail_temp_f_max)
        if user.cfg_guardrail_temp_f_max is not None
        else 75.0
    )
    return gmin, gmax


def _current_band_f(user: User) -> Tuple[float, float, float]:
    """Returns (center_f, half_width_f, span_f)."""
    lo = (
        float(user.cfg_optimal_band_f_min)
        if user.cfg_optimal_band_f_min is not None
        else 65.0
    )
    hi = (
        float(user.cfg_optimal_band_f_max)
        if user.cfg_optimal_band_f_max is not None
        else 68.0
    )
    if lo >= hi:
        hi = lo + 3.0
    span = hi - lo
    center = (lo + hi) / 2.0
    half_w = span / 2.0
    return center, half_w, span


def _clamp_band_to_guardrails(
    center: float,
    half_width: float,
    gmin: float,
    gmax: float,
) -> Tuple[float, float]:
    half_width = max(0.25, min(half_width, max((gmax - gmin) / 2.0 - 0.05, 0.25)))
    lo = center - half_width
    hi = center + half_width
    lo = max(gmin, min(lo, gmax - 2 * half_width))
    hi = min(gmax, lo + 2 * half_width)
    if hi <= lo:
        mid = max(gmin, min(center, gmax))
        lo = mid - half_width
        hi = mid + half_width
        lo = max(gmin, lo)
        hi = min(gmax, hi)
    return lo, hi


def update_optimal_band(user: User, score_date: date) -> Optional[dict]:
    """
    After a completed sleep session whose ``ended_at`` falls on ``score_date``
    (UTC calendar day): EMA-adjust optimal band from that session's mean °F,
    and scale band width from subjective morning rating (stored or default).
    """
    gmin, gmax = _guardrails_f(user)
    if gmin >= gmax:
        return None

    session_row = _latest_sleep_session_ended_on(score_date, user.id)
    if session_row is None or session_row.ended_at is None:
        return None

    readiness_value = (
        float(session_row.readiness_score)
        if session_row.readiness_score is not None
        else 0.0
    )
    alpha = alpha_from_sleep_readiness(readiness_value)

    t_center_old, half_w_old, span_old = _current_band_f(user)
    night_f = celsius_mean_between(
        session_row.started_at,
        session_row.ended_at,
        user_id=user.id,
    )

    mf = SubjectiveSleepReview.query.filter_by(
        user_id=user.id,
        feedback_for_date=score_date,
    ).first()
    rating = mf.rating if mf else 3
    width_mult = width_multiplier_from_morning_rating(rating)

    span_new = max(0.5, span_old * width_mult)
    half_w_new = span_new / 2.0

    if alpha > 0.0 and night_f is not None:
        t_center_new = (1.0 - alpha) * t_center_old + alpha * night_f
    else:
        t_center_new = t_center_old

    new_lo, new_hi = _clamp_band_to_guardrails(
        t_center_new, half_w_new, gmin, gmax,
    )

    user.cfg_optimal_band_f_min = round(new_lo, 2)
    user.cfg_optimal_band_f_max = round(new_hi, 2)

    db.session.commit()

    return {
        "user_id": user.id,
        "score_date": score_date.isoformat(),
        "alpha": alpha,
        "sleep_readiness": (
            readiness_value if session_row.readiness_score is not None else None
        ),
        "night_mean_fahrenheit": round(night_f, 3) if night_f is not None else None,
        "morning_rating_used": rating,
        "width_multiplier": width_mult,
        "optimal_band_f_min": user.cfg_optimal_band_f_min,
        "optimal_band_f_max": user.cfg_optimal_band_f_max,
    }


def run_daily_optimal_band_updates():
    """Run ``update_optimal_band`` for every user for today's wake-night score."""
    score_date = get_current_utc_time().date()
    summaries = []
    for user in User.query.all():
        out = update_optimal_band(user, score_date)
        if out:
            summaries.append(out)
    return summaries


# ---------------------------------------------------------------------------
# Sleep session detection (biometric stream present = in bed / ASLEEP)
# ---------------------------------------------------------------------------

sleep_state_lock = threading.Lock()

# AWAKE ⟷ ASLEEP: valid vitals (HR > 0, SpO₂ > 0) on a recent reading imply the
# user is in bed with the biometric board; gaps longer than the stale threshold
# imply wake / session end.
current_user_state = "AWAKE"
sleep_session_start_time = None
# Primary key of the open ``sleep_sessions`` row while ASLEEP (None otherwise).
active_sleep_session_id = None

# How far back to search for the most recent vitals-bearing reading.
SLEEP_VITALS_LOOKBACK_MINUTES = 25
# If the newest valid HR+SpO₂ sample is older than this, treat the stream as idle.
BIOMETRIC_STALE_SECONDS = 600


def _utc_isoformat_z(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    dt_utc = dt
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt_utc.astimezone(timezone.utc)
    return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_user_sleep_consciousness_state() -> str:
    with sleep_state_lock:
        return current_user_state


def get_sleep_session_resolution_context() -> Tuple[str, Optional[int]]:
    """State machine snapshot for resolving default sleep-session API queries."""
    with sleep_state_lock:
        return current_user_state, active_sleep_session_id


def snapshot_sleep_tracking() -> Dict[str, Any]:
    with sleep_state_lock:
        return {
            "current_user_state": current_user_state,
            "consciousness_state": current_user_state,
            "sleep_session_started_at": _utc_isoformat_z(sleep_session_start_time),
            "session_start_time": _utc_isoformat_z(sleep_session_start_time),
            "sleep_session_id": active_sleep_session_id,
            "session_end_time": None,
        }


def _reading_has_positive_vitals(row: Reading) -> bool:
    """Biometric stream: positive HR and SpO₂ (decoded floats)."""
    hr = _to_float(row.heart_rate)
    spo2 = _to_float(row.spo2)
    return (
        hr is not None
        and spo2 is not None
        and hr > 0.0
        and spo2 > 0.0
    )


def _latest_positive_vitals_timestamp() -> Optional[datetime]:
    """
    Timestamp of the newest reading in the lookback window that carries
    positive vitals (skips environmental rows with NULL HR/SpO₂).
    """
    now = get_current_utc_time()
    since = now - timedelta(minutes=SLEEP_VITALS_LOOKBACK_MINUTES)
    rq = Reading.query.filter(Reading.timestamp >= since)
    uid = default_ingest_user_id()
    if uid is not None:
        rq = rq.filter(or_(Reading.user_id == uid, Reading.user_id.is_(None)))
    rows_db = rq.order_by(Reading.timestamp.desc(), Reading.id.desc()).all()
    for r in rows_db:
        if not _reading_has_positive_vitals(r):
            continue
        ts = r.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts
    return None


def _biometric_stream_status() -> Tuple[bool, Optional[datetime]]:
    """(stream_active, timestamp_of_newest_positive_vitals_or_None)."""
    ts = _latest_positive_vitals_timestamp()
    if ts is None:
        return False, None
    age = (get_current_utc_time() - ts).total_seconds()
    return age <= BIOMETRIC_STALE_SECONDS, ts


def evaluate_sleep_state(app) -> Dict[str, Any]:
    """
    Update ``current_user_state`` ∈ { AWAKE, ASLEEP }.

    While positive HR and SpO₂ continue to arrive on recent readings (biometric
    stream), the user is treated as in bed and a ``SleepSession`` is open. When
    vitals go stale or absent, the session is finalized and state returns to
    AWAKE.
    """
    global current_user_state, sleep_session_start_time, active_sleep_session_id

    finalized_id: Optional[int] = None
    new_session_at: Optional[datetime] = None

    with app.app_context():
        active, vitals_ts = _biometric_stream_status()

        with sleep_state_lock:
            if active:
                if current_user_state != "ASLEEP":
                    new_session_at = vitals_ts or get_current_utc_time()
                    sleep_session_start_time = new_session_at
                    current_user_state = "ASLEEP"
            else:
                if (
                    current_user_state == "ASLEEP"
                    and active_sleep_session_id is not None
                ):
                    finalized_id = active_sleep_session_id
                current_user_state = "AWAKE"
                sleep_session_start_time = None
                active_sleep_session_id = None

        if new_session_at is not None:
            sess = SleepSession(
                started_at=new_session_at,
                user_id=default_ingest_user_id(),
            )
            db.session.add(sess)
            db.session.commit()
            with sleep_state_lock:
                active_sleep_session_id = sess.id

        if finalized_id is not None:
            finalize_sleep_session_after_wake(finalized_id)

    return snapshot_sleep_tracking()


# -----------------------------------------------------------------------------
# Biological correlators (PRV spike heuristic + gyro-independent noise context)
# -----------------------------------------------------------------------------

MICRO_PRV_RING_MAX = 21
# Acoustic correlate: excursion above calibrated baseline (same spirit as readiness noise threshold).
NOISE_SPIKE_ABOVE_BASELINE_DB = 8.0
# HRV proxy: PRV must fall by >15% within this window after a spike edge.
MICRO_AROUSAL_AFTER_SPIKE_SECONDS = 120.0
MICRO_AROUSAL_PRV_DROP_FRAC = 0.15


def prv_ms_between_hr_samples(prev_hr: Optional[float], curr_hr: Optional[float]) -> Optional[float]:
    """Beat-to-beat PRV proxy (ms) aligned with nightly chart derivation."""
    if prev_hr is None or curr_hr is None:
        return None
    try:
        a = float(prev_hr)
        b = float(curr_hr)
    except (TypeError, ValueError):
        return None
    if not (35.0 <= a <= 180.0 and 35.0 <= b <= 180.0):
        return None
    rr_prev = 60000.0 / a
    rr_curr = 60000.0 / b
    return round(abs(rr_curr - rr_prev), 2)


def _median_nonneg(vals: List[float]) -> Optional[float]:
    s = sorted(x for x in vals if isinstance(x, (int, float)) and x >= 0.0)
    if not s:
        return None
    n = len(s)
    m = n // 2
    if n % 2 == 1:
        return float(s[m])
    return (s[m - 1] + s[m]) / 2.0


def default_micro_arousal_ctx() -> Dict[str, Any]:
    return {
        "prv_ring": [],
        "prev_hr": None,
        "pending_spike_ts": None,
        "pending_spike_db": None,
        "median_prv_snapshot_ms": None,
        "last_event_ts": None,
        "noise_above_spike_threshold": False,
    }


def micro_arousal_tick(
    ctx: Dict[str, Any],
    *,
    now_utc: datetime,
    heart_rate_bpm: Optional[float],
    ambient_noise_db: Optional[float],
    noise_baseline_db: Optional[float],
) -> Optional[MicroArousalEvent]:
    """
    Micro-arousal: within ~2 min of a noise level more than ``NOISE_SPIKE_ABOVE_BASELINE_DB``
    above the running baseline, PRV/HRV-proxy falls by ``MICRO_AROUSAL_PRV_DROP_FRAC`` (15%).

    Mutates ``ctx`` in place (persisted inside ``simulated_room_state['_micro_arousal_ctx']``).
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_ts = now_utc.timestamp()

    ring = list(ctx.get("prv_ring") or [])

    above = False
    if ambient_noise_db is not None and noise_baseline_db is not None:
        try:
            above = float(ambient_noise_db) > (
                float(noise_baseline_db) + NOISE_SPIKE_ABOVE_BASELINE_DB
            )
        except (TypeError, ValueError):
            above = False
    prev_above = ctx.get("noise_above_spike_threshold") is True
    rising_edge = above and not prev_above

    prv_ms = prv_ms_between_hr_samples(ctx.get("prev_hr"), heart_rate_bpm)

    created: Optional[MicroArousalEvent] = None

    # Arm window on spike **edge** relative to calibrated baseline (>8 dB).
    if rising_edge and ambient_noise_db is not None:
        median_snap = _median_nonneg(ring)
        ctx["pending_spike_ts"] = now_ts
        ctx["pending_spike_db"] = float(ambient_noise_db)
        ctx["median_prv_snapshot_ms"] = median_snap

    pst = ctx.get("pending_spike_ts")
    if pst is not None:
        elapsed = now_ts - float(pst)
        if elapsed > MICRO_AROUSAL_AFTER_SPIKE_SECONDS:
            ctx["pending_spike_ts"] = None
            ctx["pending_spike_db"] = None
            ctx["median_prv_snapshot_ms"] = None
        elif (
            elapsed <= MICRO_AROUSAL_AFTER_SPIKE_SECONDS
            and prv_ms is not None
        ):
            median_snap = ctx.get("median_prv_snapshot_ms")
            if median_snap is not None:
                mv = float(median_snap)
                prv_val = float(prv_ms)
                floor_ms = mv * (1.0 - MICRO_AROUSAL_PRV_DROP_FRAC)
                if mv >= 1e-3 and prv_val < floor_ms:
                    last_ev = ctx.get("last_event_ts")
                    if last_ev is None or (now_ts - float(last_ev)) > 35.0:
                        spike_noise = float(ctx.get("pending_spike_db") or 0.0)
                        prv_drop = round(mv - prv_val, 2)
                        try:
                            thresh = (
                                float(noise_baseline_db)
                                + NOISE_SPIKE_ABOVE_BASELINE_DB
                                if noise_baseline_db is not None
                                else None
                            )
                        except (TypeError, ValueError):
                            thresh = None
                        if thresh is not None:
                            label = (
                                f"Micro-arousal — noise {spike_noise:.1f} dB "
                                f"(threshold {thresh:.1f} dB)"
                            )
                        else:
                            label = f"Micro-arousal — noise {spike_noise:.1f} dB"
                        label = label[:158]
                        created = MicroArousalEvent(
                            spike_noise_db=spike_noise,
                            prv_median_before_ms=mv,
                            prv_observed_ms=prv_val,
                            prv_drop_ms=prv_drop,
                            disruption_label=label,
                        )
                        ctx["last_event_ts"] = now_ts
                        ctx["pending_spike_ts"] = None
                        ctx["pending_spike_db"] = None
                        ctx["median_prv_snapshot_ms"] = None

    if prv_ms is not None:
        ring.append(float(prv_ms))
        while len(ring) > MICRO_PRV_RING_MAX:
            ring.pop(0)

    ctx["prv_ring"] = ring
    ctx["noise_above_spike_threshold"] = above

    if heart_rate_bpm is not None:
        try:
            fh = float(heart_rate_bpm)
        except (TypeError, ValueError):
            fh = None
        if fh is not None and 35.0 <= fh <= 180.0:
            ctx["prev_hr"] = fh

    return created


def optimal_temp_upper_celsius(ref_user: Optional[User]) -> float:
    """Upper bound (°C) for the persisted optimal Fahrenheit sleep band."""
    if ref_user is not None:
        hi_f = float(ref_user.cfg_optimal_band_f_max or 68.0)
    else:
        hi_f = 68.0
    return round((hi_f - 32.0) * (5.0 / 9.0), 4)
