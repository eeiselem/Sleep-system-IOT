from __future__ import annotations

"""Sleep-session scoring utilities.

These helpers finalize a session after wake and compute the
readiness fields stored on each sleep session row.
"""

from typing import List, Optional

import config
from db import db
from schemas.sleep_session import SleepSession
from schemas.reading import Reading
from crud.reading import read_sleep_session_between
from utils import (
    get_current_utc_time,
    mean_or_none,
    restlessness_score_from_raw,
    to_float_or_none,
)

# Formula tag used when logging subjective-vs-score mismatch.
READINESS_SCORE_FORMULA_VERSION = "0.40*eff + 0.40*hrv_hr + 0.20*spo2_v2"


def _map_linear_clamped(value: float, low: float, high: float) -> float:
    # Map value to 0..100 and clamp hard at the ends.
    if high <= low:
        return 0.0
    pct = ((value - low) / (high - low)) * 100.0
    return min(max(pct, 0.0), 100.0)


def readings_for_sleep_session(sess: SleepSession) -> List[Reading]:
    # Pull all readings within one session window.
    end = (
        sess.ended_at if sess.ended_at is not None else get_current_utc_time()
    )
    uid = getattr(sess, "user_id", None)
    return read_sleep_session_between(sess.started_at, end, user_id=uid)


def finalize_sleep_session_after_wake(
    session_id: int,
) -> Optional[SleepSession]:
    # Close session if needed, then calculate readiness.
    sess = db.session.get(SleepSession, session_id)
    if sess is None:
        return None
    if sess.ended_at is None:
        sess.ended_at = get_current_utc_time()
        db.session.commit()
    return compute_sleep_readiness_for_session(session_id)


def compute_sleep_readiness_for_session(
    session_id: int,
) -> Optional[SleepSession]:
    # Compute and store readiness stats for one finished session.
    sess = db.session.get(SleepSession, session_id)
    if sess is None or sess.ended_at is None:
        return None
    if sess.readiness_score is not None:
        return sess

    readings: List[Reading] = readings_for_sleep_session(sess)
    if not readings:
        # Keep deterministic zeros for empty sessions.
        sess.readiness_score = 0.0
        sess.sleep_efficiency_pct = 0.0
        sess.sleep_efficiency_score = 0.0
        sess.avg_prv_ms = 0.0
        sess.prv_score = 0.0
        sess.spo2_drop_count = 0
        sess.noise_spike_count = 0
        sess.disturbance_score = 0.0
        sess.sample_count = 0
        sess.calculated_at = get_current_utc_time()
        db.session.commit()
        return sess

    gyro_values = [to_float_or_none(r.gyro_variance) for r in readings]
    gyro_values = [v for v in gyro_values if v is not None]
    # Restful efficiency is 100=still, 0=restless.
    restful_efficiency_samples = []
    for v in gyro_values:
        s = restlessness_score_from_raw(v)
        if s is not None:
            restful_efficiency_samples.append(s)
    if restful_efficiency_samples:
        # Percent of samples in excellent stillness band.
        restful_count = sum(1 for s in restful_efficiency_samples if s >= 80.0)
        sleep_efficiency_pct = (
            restful_count / len(restful_efficiency_samples)
        ) * 100.0
    else:
        sleep_efficiency_pct = 0.0
    sleep_efficiency_score = min(max(sleep_efficiency_pct, 0.0), 100.0)

    heart_rates = [to_float_or_none(r.heart_rate) for r in readings]
    # Approximate beat-to-beat variability from neighboring RR intervals.
    rr_intervals = [
        (60000.0 / hr)
        for hr in heart_rates
        if hr is not None and 35.0 <= hr <= 180.0
    ]
    prv_deltas = []
    for idx in range(1, len(rr_intervals)):
        prv_deltas.append(abs(rr_intervals[idx] - rr_intervals[idx - 1]))
    avg_prv_ms = sum(prv_deltas) / len(prv_deltas) if prv_deltas else 0.0
    prv_score = min(max((avg_prv_ms / 120.0) * 100.0, 0.0), 100.0)

    spo2_values = [to_float_or_none(r.spo2) for r in readings]
    clean_spo2_values = [v for v in spo2_values if v is not None]
    avg_spo2 = mean_or_none(clean_spo2_values) or 0.0
    # SpO2 score mapped to 0..100.
    spo2_score = _map_linear_clamped(avg_spo2, 88.0, 97.0)
    spo2_drop_count = sum(
        1
        for v in clean_spo2_values
        if v < config.READINESS_SPO2_DROP_THRESHOLD
    )

    noise_values = [to_float_or_none(r.ambient_noise) for r in readings]
    noise_values = [v for v in noise_values if v is not None]
    if noise_values:
        noise_baseline = sum(noise_values) / len(noise_values)
        noise_threshold = (
            noise_baseline + config.READINESS_NOISE_SPIKE_DELTA_DB
        )
    else:
        noise_threshold = float("inf")
    noise_spike_count = sum(1 for v in noise_values if v > noise_threshold)

    disturbance_events = spo2_drop_count + noise_spike_count
    disturbance_penalty = min(disturbance_events * 5.0, 100.0)
    # Keep for old telemetry fields.
    disturbance_score = 100.0 - disturbance_penalty
    # HR score from session mean, then combine with PRV.
    avg_hr = (
        mean_or_none(
            [
                hr
                for hr in heart_rates
                if hr is not None and 30.0 <= hr <= 200.0
            ]
        )
        or 0.0
    )
    # 45-65 best, 35-80 ok-ish, outside low.
    if 45.0 <= avg_hr <= 65.0:
        heart_rate_score = 100.0
    elif 35.0 <= avg_hr <= 80.0:
        heart_rate_score = 60.0
    else:
        heart_rate_score = 20.0
    hrv_heart_score = (prv_score + heart_rate_score) / 2.0

    # Final blend: 40% efficiency, 40% HRV/HR, 20% SpO2.
    readiness_score = (
        (config.READINESS_WEIGHT_SLEEP_EFFICIENCY * sleep_efficiency_score)
        + (config.READINESS_WEIGHT_HRV_HEART * hrv_heart_score)
        + (config.READINESS_WEIGHT_SPO2 * spo2_score)
    )
    readiness_score = round(min(max(readiness_score, 0.0), 100.0), 2)

    sess.readiness_score = readiness_score
    sess.sleep_efficiency_pct = round(sleep_efficiency_pct, 2)
    sess.sleep_efficiency_score = round(sleep_efficiency_score, 2)
    sess.avg_prv_ms = round(avg_prv_ms, 2)
    sess.prv_score = round(prv_score, 2)
    sess.spo2_drop_count = spo2_drop_count
    sess.noise_spike_count = noise_spike_count
    sess.disturbance_score = round(disturbance_score, 2)
    sess.sample_count = len(readings)
    sess.calculated_at = get_current_utc_time()
    db.session.commit()
    return sess
