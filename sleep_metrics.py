from __future__ import annotations

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
    if high <= low:
        return 0.0
    pct = ((value - low) / (high - low)) * 100.0
    return min(max(pct, 0.0), 100.0)


def _score_environment_stability(readings: List[Reading]) -> float:
    # Extra environment score (0-100), not part of readiness blend.

    def score_temp_f(v_f: float) -> float:
        if 60.0 <= v_f <= 75.0:
            return 100.0
        if 55.0 <= v_f <= 80.0:
            return 60.0
        return 20.0

    def score_noise_db(v: float) -> float:
        if v < 40.0:
            return 100.0
        if v <= 50.0:
            return 60.0
        return 20.0

    def score_lux(v: float) -> float:
        if v < 2.0:
            return 100.0
        if v <= 40.0:
            return 60.0
        return 20.0

    def score_aqi(v: float) -> float:
        if 0.0 <= v <= 50.0:
            return 100.0
        if v <= 100.0:
            return 60.0
        return 20.0

    temp_scores: List[float] = []
    noise_scores: List[float] = []
    light_scores: List[float] = []
    aqi_scores: List[float] = []

    for r in readings:
        t_c = to_float_or_none(r.temperature)
        if t_c is not None:
            t_f = (t_c * (9.0 / 5.0)) + 32.0
            temp_scores.append(score_temp_f(t_f))

        nz = to_float_or_none(r.ambient_noise)
        if nz is not None:
            noise_scores.append(score_noise_db(nz))

        lx = to_float_or_none(r.ambient_light)
        if lx is not None:
            light_scores.append(score_lux(lx))

        aq = to_float_or_none(r.air_quality)
        if aq is not None:
            aqi_scores.append(score_aqi(aq))

    channel_means = [
        mean_or_none(temp_scores),
        mean_or_none(noise_scores),
        mean_or_none(light_scores),
        mean_or_none(aqi_scores),
    ]
    channel_means = [m for m in channel_means if m is not None]
    if not channel_means:
        return 0.0
    return min(max(sum(channel_means) / len(channel_means), 0.0), 100.0)


def readings_for_sleep_session(sess: SleepSession) -> List[Reading]:
    end = (
        sess.ended_at
        if sess.ended_at is not None
        else get_current_utc_time()
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
        sess.readiness_score = 0.0
        sess.sleep_efficiency_pct = 0.0
        sess.sleep_efficiency_score = 0.0
        sess.avg_prv_ms = 0.0
        sess.prv_score = 0.0
        sess.spo2_drop_count = 0
        sess.noise_spike_count = 0
        sess.disturbance_score = 0.0
        sess.environment_stability_score = 0.0
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

    spo2_values = [to_float_or_none(r.spo2) for r in readings]
    clean_spo2_values = [v for v in spo2_values if v is not None]
    avg_spo2 = mean_or_none(clean_spo2_values) or 0.0
    # SpO2 score mapped to 0..100.
    spo2_score = _map_linear_clamped(avg_spo2, 88.0, 97.0)
    spo2_drop_count = sum(
        1 for v in clean_spo2_values if v < config.READINESS_SPO2_DROP_THRESHOLD
    )

    noise_values = [to_float_or_none(r.ambient_noise) for r in readings]
    noise_values = [v for v in noise_values if v is not None]
    if noise_values:
        noise_baseline = sum(noise_values) / len(noise_values)
        noise_threshold = noise_baseline + config.READINESS_NOISE_SPIKE_DELTA_DB
    else:
        noise_threshold = float("inf")
    noise_spike_count = sum(
        1 for v in noise_values
        if v > noise_threshold
    )

    disturbance_events = spo2_drop_count + noise_spike_count
    disturbance_penalty = min(disturbance_events * 5.0, 100.0)
    # Keep for old telemetry fields.
    disturbance_score = 100.0 - disturbance_penalty
    environment_stability_score = _score_environment_stability(readings)

    # HR score from session mean, then combine with PRV.
    avg_hr = mean_or_none(
        [
            hr
            for hr in heart_rates
            if hr is not None and 30.0 <= hr <= 200.0
        ]
    ) or 0.0
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
    sess.environment_stability_score = round(environment_stability_score, 2)
    sess.sample_count = len(readings)
    sess.calculated_at = get_current_utc_time()
    db.session.commit()
    return sess
