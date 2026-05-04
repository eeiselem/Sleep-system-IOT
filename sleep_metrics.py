"""Sleep readiness computation from persisted session bounds (FSM-derived)."""

from __future__ import annotations

from typing import List, Optional

from db import db
from schemas.sleep_session import SleepSession
from schemas.reading import Reading
from crud.reading import read_sleep_session_between
from utils import get_current_utc_time, restlessness_score_from_raw

# Audit label for subjective-vs-algorithm discrepancy rows; must match the blend in
# ``compute_sleep_readiness_for_session`` (0.45 / 0.35 / 0.20).
READINESS_SCORE_FORMULA_VERSION = "0.45*eff + 0.35*prv + 0.20*dist_v1"


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def readings_for_sleep_session(sess: SleepSession) -> List[Reading]:
    end = sess.ended_at if sess.ended_at is not None else get_current_utc_time()
    uid = getattr(sess, "user_id", None)
    return read_sleep_session_between(sess.started_at, end, user_id=uid)


def finalize_sleep_session_after_wake(session_id: int) -> Optional[SleepSession]:
    """Set ``ended_at`` if missing; then compute readiness."""
    sess = db.session.get(SleepSession, session_id)
    if sess is None:
        return None
    if sess.ended_at is None:
        sess.ended_at = get_current_utc_time()
        db.session.commit()
    return compute_sleep_readiness_for_session(session_id)


def compute_sleep_readiness_for_session(session_id: int) -> Optional[SleepSession]:
    """
    Populate readiness columns on ``SleepSession`` using all readings strictly
    between session start (ASLEEP) and end (AWAKE wake); requires ``ended_at``.

    ``readiness_score`` weights are documented inline where the score is computed
    (see block above ``readiness_score = ...``).
    """
    sess = db.session.get(SleepSession, session_id)
    if sess is None or sess.ended_at is None:
        return None
    if sess.readiness_score is not None:
        return sess

    readings: List[Reading] = readings_for_sleep_session(sess)
    if not readings:
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

    gyro_values = [_to_float(r.gyro_variance) for r in readings]
    gyro_values = [v for v in gyro_values if v is not None]
    # Per-sample restful efficiency (100 = still); see ``utils.restlessness_score_from_raw``.
    restful_efficiency_samples = []
    for v in gyro_values:
        s = restlessness_score_from_raw(v)
        if s is not None:
            restful_efficiency_samples.append(s)
    if restful_efficiency_samples:
        # Share of samples in the top "excellent still" band (aligned with old <=20 motion-stress bucket).
        restful_count = sum(1 for s in restful_efficiency_samples if s >= 80.0)
        sleep_efficiency_pct = (restful_count / len(restful_efficiency_samples)) * 100.0
    else:
        sleep_efficiency_pct = 0.0
    sleep_efficiency_score = min(max(sleep_efficiency_pct, 0.0), 100.0)

    heart_rates = [_to_float(r.heart_rate) for r in readings]
    rr_intervals = [
        (60000.0 / hr)
        for hr in heart_rates
        if hr is not None and 35.0 <= hr <= 180.0
    ]
    prv_deltas = []
    for idx in range(1, len(rr_intervals)):
        prv_deltas.append(abs(rr_intervals[idx] - rr_intervals[idx - 1]))
    avg_prv_ms = (
        sum(prv_deltas) / len(prv_deltas)
        if prv_deltas else 0.0
    )
    prv_score = min(max((avg_prv_ms / 120.0) * 100.0, 0.0), 100.0)

    spo2_values = [_to_float(r.spo2) for r in readings]
    spo2_drop_count = sum(
        1 for v in spo2_values
        if v is not None and v < 92.0
    )

    noise_values = [_to_float(r.ambient_noise) for r in readings]
    noise_values = [v for v in noise_values if v is not None]
    if noise_values:
        noise_baseline = sum(noise_values) / len(noise_values)
        noise_threshold = noise_baseline + 8.0
    else:
        noise_threshold = float("inf")
    noise_spike_count = sum(
        1 for v in noise_values
        if v > noise_threshold
    )

    disturbance_events = spo2_drop_count + noise_spike_count
    disturbance_penalty = min(disturbance_events * 5.0, 100.0)
    disturbance_score = 100.0 - disturbance_penalty

    # --- Sleep score (readiness_score) — weighted blend (all sub-scores are 0–100, higher is better) ---
    #
    #   • 45% — sleep_efficiency_score (= sleep_efficiency_pct after clamp). sleep_efficiency_pct is the
    #           percentage of gyro samples in the session whose **restful efficiency** is ≥ 80
    #           (``utils.restlessness_score_from_raw``: 100 = still, 0 = restless).
    #
    #   • 35% — prv_score from mean absolute beat-to-beat RR interval change (PRV-style): larger
    #           average delta (ms) across consecutive valid HR samples → higher prv_score, capped by
    #           ``prv_score = min(100, (avg_prv_ms / 120.0) * 100)``. Not HRV RMSSD from ECG; uses HR-derived RR steps only.
    #
    #   • 20% — disturbance_score: starts at 100; each SpO₂ sample below 92% or each ambient-noise spike
    #           (> baseline + 8 dB) costs 5 points until a floor of 0 (``disturbance_penalty`` cap 100).
    #
    # Final: readiness_score = 0.45*sleep_efficiency_score + 0.35*prv_score + 0.20*disturbance_score,
    # then clamped to [0, 100]. **Motion / restfulness is not a separate weighted term** — it feeds only the
    # 45% bucket via the share of samples with restful efficiency ≥ 80.
    #
    readiness_score = (
        (0.45 * sleep_efficiency_score) +
        (0.35 * prv_score) +
        (0.20 * disturbance_score)
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
