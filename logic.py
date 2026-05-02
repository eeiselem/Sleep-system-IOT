"""Sleep / comfort tuning helpers (EMA optimal band updates)."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from statistics import pstdev
from typing import Any, Dict, List, Optional, Tuple

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


def _latest_sleep_session_ended_on(cal_date: date) -> Optional[SleepSession]:
    candidates = (
        SleepSession.query.filter(SleepSession.ended_at.isnot(None))
        .order_by(SleepSession.ended_at.desc())
        .all()
    )
    for s in candidates:
        if s.ended_at is not None and _utc_calendar_date(s.ended_at) == cal_date:
            return s
    return None


def celsius_mean_between(window_start_utc: datetime, window_end_utc: datetime) -> Optional[float]:
    """Mean room temperature over [start, end] UTC inclusive, converted to °F (same units as before)."""
    if window_start_utc.tzinfo is None:
        window_start_utc = window_start_utc.replace(tzinfo=timezone.utc)
    else:
        window_start_utc = window_start_utc.astimezone(timezone.utc)
    if window_end_utc.tzinfo is None:
        window_end_utc = window_end_utc.replace(tzinfo=timezone.utc)
    else:
        window_end_utc = window_end_utc.astimezone(timezone.utc)

    readings = (
        Reading.query.filter(Reading.timestamp >= window_start_utc)
        .filter(Reading.timestamp <= window_end_utc)
        .all()
    )
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

    session_row = _latest_sleep_session_ended_on(score_date)
    if session_row is None or session_row.ended_at is None:
        return None

    readiness_value = (
        float(session_row.readiness_score)
        if session_row.readiness_score is not None
        else 0.0
    )
    alpha = alpha_from_sleep_readiness(readiness_value)

    t_center_old, half_w_old, span_old = _current_band_f(user)
    night_f = celsius_mean_between(session_row.started_at, session_row.ended_at)

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
# Heuristic consciousness / sleep onset (gyro quiet + HR deceleration)
# ---------------------------------------------------------------------------

sleep_state_lock = threading.Lock()

# AWAKE → SETTLING → ASLEEP (heuristic); violent gyro snaps back to AWAKE.
current_user_state = "AWAKE"
sleep_session_start_time = None
# Primary key of the open ``sleep_sessions`` row while ASLEEP (None otherwise).
active_sleep_session_id = None

GYRO_VIOLENT_SPIKE = 0.28
GYRO_SETTLE_MAX = 0.05
SLEEP_LOOKBACK_MINUTES = 10
SLEEP_SUSTAINED_MINUTES = 5
SLEEP_WINDOW_NEED_SECONDS = (
    max(295, int(SLEEP_SUSTAINED_MINUTES * 60) - 5)
)

RESTING_HR_LO = 50.0
RESTING_HR_HI = 78.0
HALVES_DECEL_MIN_BPM = 4.0
RESTING_HALF_STDD_MAX = 6.0
MIN_HR_SAMPLES_FOR_ASLEEP = 8


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


def _read_rows_last_window(minutes_back: float) -> List[Dict[str, Any]]:
    now = get_current_utc_time()
    since = now - timedelta(minutes=minutes_back)
    rows_db = (
        Reading.query.filter(Reading.timestamp >= since)
        .order_by(Reading.timestamp.asc())
        .all()
    )
    out = []
    for r in rows_db:
        ts = r.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append(
            {
                "timestamp": ts,
                "gyro": _to_float(r.gyro_variance),
                "hr": _to_float(r.heart_rate),
            }
        )
    return out


def _gyro_violent_spike(samples: List[Dict[str, Any]]) -> bool:
    for pt in samples:
        g = pt["gyro"]
        if g is not None and g >= GYRO_VIOLENT_SPIKE:
            return True
    return False


def _low_gyro_suffix_window_ok(samples: List[Dict[str, Any]]) -> bool:
    """Last ``SLEEP_SUSTAINED_MINUTES`` contiguous window from latest sample."""

    if not samples:
        return False
    t_end = samples[-1]["timestamp"]
    cutoff = t_end - timedelta(minutes=SLEEP_SUSTAINED_MINUTES)
    window = [pt for pt in samples if pt["timestamp"] >= cutoff]
    if not window:
        return False
    span_sec = (
        window[-1]["timestamp"] - window[0]["timestamp"]
    ).total_seconds()
    if span_sec < SLEEP_WINDOW_NEED_SECONDS:
        return False
    for pt in window:
        g = pt["gyro"]
        if g is None or g >= GYRO_SETTLE_MAX:
            return False
    return True


def _hr_decelerating_and_stable_rest(samples: List[Dict[str, Any]]) -> bool:
    """Last sustained window ending at latest stamp: halves + resting band."""

    if not samples:
        return False
    t_end = samples[-1]["timestamp"]
    cutoff = t_end - timedelta(minutes=SLEEP_SUSTAINED_MINUTES)
    hrs = []
    for pt in samples:
        if pt["timestamp"] < cutoff:
            continue
        hr = pt["hr"]
        if hr is None or not (38.0 <= hr <= 120.0):
            continue
        hrs.append(hr)

    if len(hrs) < MIN_HR_SAMPLES_FOR_ASLEEP:
        return False
    mid = max(4, len(hrs) // 2)
    if len(hrs) - mid < 4:
        return False
    first_half = hrs[:mid]
    second_half = hrs[mid:]
    avg_first = sum(first_half) / len(first_half)
    avg_rest = sum(second_half) / len(second_half)
    if avg_rest > RESTING_HR_HI or avg_rest < RESTING_HR_LO:
        return False
    std_rest = pstdev(second_half) if len(second_half) > 1 else 0.0
    if avg_first <= avg_rest + HALVES_DECEL_MIN_BPM:
        return False
    if std_rest > RESTING_HALF_STDD_MAX:
        return False
    return True


def evaluate_sleep_state(app) -> Dict[str, Any]:
    """
    Update ``current_user_state`` ∈ { AWAKE, SETTLING, ASLEEP } using the last
    ``SLEEP_LOOKBACK_MINUTES`` of gyro + HR samples.

    Persists a ``SleepSession`` row at ASLEEP onset and sets ``ended_at`` plus
    readiness metrics when a violent gyro returns the user to AWAKE.
    """
    global current_user_state, sleep_session_start_time, active_sleep_session_id

    finalized_id: Optional[int] = None
    asleep_start_candidate: Optional[datetime] = None

    with app.app_context():
        seq = _read_rows_last_window(SLEEP_LOOKBACK_MINUTES)

        with sleep_state_lock:
            if _gyro_violent_spike(seq):
                if (
                    current_user_state == "ASLEEP"
                    and active_sleep_session_id is not None
                ):
                    finalized_id = active_sleep_session_id
                current_user_state = "AWAKE"
                sleep_session_start_time = None
                active_sleep_session_id = None
            else:
                low_stable = _low_gyro_suffix_window_ok(seq)

                if current_user_state == "AWAKE":
                    if low_stable:
                        current_user_state = "SETTLING"

                elif current_user_state == "SETTLING":
                    if not low_stable:
                        current_user_state = "AWAKE"
                        sleep_session_start_time = None
                    elif _hr_decelerating_and_stable_rest(seq):
                        current_user_state = "ASLEEP"
                        asleep_start_candidate = get_current_utc_time()
                        sleep_session_start_time = asleep_start_candidate

        if asleep_start_candidate is not None:
            sess = SleepSession(started_at=asleep_start_candidate)
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
