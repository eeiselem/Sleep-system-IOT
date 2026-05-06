import threading
import time
from datetime import timedelta

"""Room simulation and actuator intent logic.

This file keeps the control-center simulation state in one place,
including background workers that refresh derived room signals.
"""

from sqlalchemy import or_

import config
from crud import reading
from db import db
from logic import (
    default_ingest_user_id,
    default_micro_arousal_ctx,
    evaluate_sleep_state,
    get_user_sleep_consciousness_state,
    micro_arousal_tick,
    run_daily_optimal_band_updates,
    snapshot_sleep_tracking,
)
from schemas.reading import Reading
from schemas.sleep_session import SleepSession
from schemas.user import User
from sleep_metrics import compute_sleep_readiness_for_session
from timefmt import utc_isoformat_z
from utils import get_current_utc_time, to_float_or_none

_flask_app = None


def init_room_sim(app):
    # Attach Flask app reference so worker threads can use app context safely.
    global _flask_app
    _flask_app = app


total_records_cache = None
sim_room_lock = threading.Lock()
# Shared mutable state for simulation endpoints and worker threads.
simulated_room_state = {
    "room_id": "bedroom-sim-01",
    "state": "initializing",
    "occupied": True,
    "sleep_mode_active": False,
    "sleep_onset_confirmed": False,
    "sleep_signal_score": 0,
    "last_drift_detected": [],
    # Demo flags from /api/dev/simulation.
    "dev_simulation": {
        "force_high_temperature": False,
        "force_high_noise": False,
        "force_low_temperature": False,
        "force_voc_spike": False,
    },
    "simulated_hardware": {
        "cooling": False,
        "heater": False,
        "white_noise": False,
        "fan": False,
        "air_filtration_high_fan": False,
    },
    # Default ambient light setup.
    "lighting": {
        "state": "ambient",
        "brightness_percent": 8,
        "lumens_nominal": 5,
        "lumens_range_lm": {"min": 0, "max": 10},
    },
    # Default temp target.
    "hvac": {"mode": "idle", "target_temperature_c": 17.0},
    "ventilation": {"mode": "normal", "fan_percent": 20},
    "environment": {
        "temperature_c": None,
        "humidity_percent": None,
        "ambient_noise_db": None,
        "voc_index": None,
    },
    # Baseline defaults used by control loop.
    "baselines": {
        "temperature_c": (
            config.SCI_TEMP_BAND_C_MIN + config.SCI_TEMP_BAND_C_MAX
        )
        / 2.0,
        "temperature_band_c": {
            "min": config.SCI_TEMP_BAND_C_MIN,
            "max": config.SCI_TEMP_BAND_C_MAX,
        },
        "humidity_percent": (
            config.SCI_HUMIDITY_PCT_MIN + config.SCI_HUMIDITY_PCT_MAX
        )
        / 2.0,
        "humidity_band_pct": {
            "min": config.SCI_HUMIDITY_PCT_MIN,
            "max": config.SCI_HUMIDITY_PCT_MAX,
        },
        "ambient_noise_db": None,
        "voc_index": None,
        "heart_rate_bpm": None,
        "ambient_light_lux": 5.0,
        "ambient_light_lux_band": {
            "min": config.SCI_LUX_MIN,
            "max": config.SCI_LUX_MAX,
        },
    },
    "last_transition": None,
    "recent_changes": [],
}
monitor_thread_started = False
readiness_thread_started = False
optimal_band_thread_started = False
_last_optimal_band_utc_date = None


def session_mean_heart_rate_bpm(open_session_pk):
    # Mean HR over the active sleep session window.
    if open_session_pk is None:
        return None
    sess = db.session.get(SleepSession, open_session_pk)
    if sess is None or sess.started_at is None:
        return None
    rows = reading.read_sleep_session_between(
        sess.started_at,
        get_current_utc_time(),
        user_id=getattr(sess, "user_id", None),
    )
    vals = []
    for row in rows:
        h = to_float_or_none(row.heart_rate)
        if h is not None and 35.0 <= float(h) <= 180.0:
            vals.append(float(h))
    if not vals:
        return None
    return sum(vals) / len(vals)


def compute_sunrise_sequence(now_utc, wake_time_str):
    # Build a simple sunrise ramp model for the lighting card.
    sunrise_window_minutes = 30
    if not wake_time_str:
        wake_time_str = "07:00"

    try:
        wake_hour, wake_minute = wake_time_str.split(":")
        wake_hour = int(wake_hour)
        wake_minute = int(wake_minute)
    except (ValueError, TypeError):
        wake_hour, wake_minute = 7, 0

    wake_today = now_utc.replace(
        hour=wake_hour,
        minute=wake_minute,
        second=0,
        microsecond=0,
    )
    if now_utc > wake_today + timedelta(minutes=20):
        next_wake = wake_today + timedelta(days=1)
    else:
        next_wake = wake_today

    minutes_to_wake = (next_wake - now_utc).total_seconds() / 60.0
    raw_progress = (
        sunrise_window_minutes - max(minutes_to_wake, 0)
    ) / sunrise_window_minutes
    progress = min(max(raw_progress, 0.0), 1.0)
    active = 0.0 < progress <= 1.0

    lumen = int(40 + (760 * progress))
    color_temp_k = int(2200 + (2800 * progress))
    brightness_percent = int(5 + (95 * progress))
    phase = "pre_sunrise"
    if active:
        phase = "sunrise_ramp"
    elif progress >= 1.0:
        phase = "wake_now"

    return {
        "active": active,
        "phase": phase,
        "wake_time": f"{wake_hour:02d}:{wake_minute:02d}",
        "minutes_to_wake": round(minutes_to_wake, 1),
        # keep this in UTC for frontend math
        "next_wake_unix_ms": int(next_wake.timestamp() * 1000),
        "progress_percent": round(progress * 100, 1),
        "lumen": lumen,
        "color_temperature_k": color_temp_k,
        "brightness_percent": brightness_percent,
    }


def append_room_change(event_type, reason, payload):
    # Keep a small rolling event log for control page polling.
    event = {
        "timestamp": utc_isoformat_z(get_current_utc_time()),
        "event_type": event_type,
        "reason": reason,
        "payload": payload,
    }
    simulated_room_state["recent_changes"].append(event)
    simulated_room_state["recent_changes"] = simulated_room_state[
        "recent_changes"
    ][-20:]
    simulated_room_state["last_transition"] = event["timestamp"]


def compute_simulated_hardware(
    interventions_ok,
    dev_sim,
    *,
    voc_drift_active=False,
    biological_cooling=False,
    temp_above_optimal=False,
    temp_below_optimal=False,
    noise_spike=False,
    noise_constant_high=False,
):
    # Convert current signals + dev flags to actuator states.
    out = {
        "cooling": False,
        "heater": False,
        "white_noise": False,
        "fan": False,
        "air_filtration_high_fan": False,
    }
    ds = dev_sim if isinstance(dev_sim, dict) else {}
    ft = ds.get("force_high_temperature") is True
    fn = ds.get("force_high_noise") is True
    fl = ds.get("force_low_temperature") is True
    fv = ds.get("force_voc_spike") is True

    # In demo mode scenarios (except clear), ignore live HVAC/noise rules.
    # This keeps the dashboard aligned with the selected scenario.
    dev_scenario_active = any((ft, fn, fl, fv))

    if ft:
        out["cooling"] = True
    if fn:
        out["white_noise"] = True
    if fl:
        out["heater"] = True
    if fv:
        out["air_filtration_high_fan"] = True

    if voc_drift_active and not dev_scenario_active:
        out["air_filtration_high_fan"] = True

    if interventions_ok and not dev_scenario_active:
        if temp_above_optimal:
            out["cooling"] = True
        if temp_below_optimal:
            out["heater"] = True
        if noise_spike or noise_constant_high:
            out["white_noise"] = True

    if biological_cooling and temp_above_optimal and not dev_scenario_active:
        out["cooling"] = True

    if out["air_filtration_high_fan"]:
        out["fan"] = False
    elif out["cooling"] or out["white_noise"]:
        out["fan"] = True

    return out


def refresh_simulated_hardware_locked():
    # Recompute simulated_hardware (caller must hold sim_room_lock).
    dev = simulated_room_state.get("dev_simulation") or {}
    interventions_ok = bool(simulated_room_state.get("sleep_mode_active"))
    hints = simulated_room_state.get("_autonomy_hints") or {}
    ins = simulated_room_state.get("_hw_inputs") or {}
    simulated_room_state["simulated_hardware"] = compute_simulated_hardware(
        interventions_ok,
        dev,
        voc_drift_active=bool(hints.get("voc_drift_active")),
        biological_cooling=bool(hints.get("biological_cooling_high_hr_hot")),
        temp_above_optimal=bool(ins.get("temp_above_optimal")),
        temp_below_optimal=bool(ins.get("temp_below_optimal")),
        noise_spike=bool(ins.get("noise_spike")),
        noise_constant_high=bool(ins.get("noise_constant_high")),
    )


def calculate_sleep_readiness_worker():
    # Repair worker: backfill readiness for ended sessions.
    while True:
        with _flask_app.app_context():
            pending = SleepSession.query.filter(
                SleepSession.ended_at.isnot(None),
                SleepSession.readiness_score.is_(None),
            ).all()
            for s in pending:
                compute_sleep_readiness_for_session(s.id)
        time.sleep(1800)


def update_optimal_band_worker():
    # Daily optimal-band update (runs once after 08:35 UTC).
    global _last_optimal_band_utc_date
    while True:
        time.sleep(60)
        with _flask_app.app_context():
            now_utc = get_current_utc_time()
            today = now_utc.date()
            if now_utc.hour < 8 or (now_utc.hour == 8 and now_utc.minute < 35):
                continue
            if _last_optimal_band_utc_date == today:
                continue
            run_daily_optimal_band_updates()
            _last_optimal_band_utc_date = today


def evaluate_sleep_and_environment():
    # Main background worker: refresh sleep state and simulated environment.
    while True:
        time.sleep(10)
        with _flask_app.app_context():
            du = default_ingest_user_id()
            rq = Reading.query
            if du is not None:
                rq = rq.filter(
                    or_(Reading.user_id == du, Reading.user_id.is_(None))
                )
            latest = rq.order_by(
                Reading.timestamp.desc(), Reading.id.desc()
            ).first()
            if latest is None:
                continue

            evaluate_sleep_state(_flask_app)
            consciousness = get_user_sleep_consciousness_state()
            asleep = consciousness == "ASLEEP"
            sleep_snapshot = snapshot_sleep_tracking()
            sleep_signal_score_map = {"AWAKE": 0, "ASLEEP": 5}
            sleep_signal_score = sleep_signal_score_map.get(consciousness, 0)

            temp = to_float_or_none(latest.temperature)
            humid = to_float_or_none(latest.humidity)
            noise = to_float_or_none(latest.ambient_noise)
            voc = to_float_or_none(latest.air_quality)
            hr = to_float_or_none(latest.heart_rate)

            # Use first user's saved optimal band as HVAC target.
            ref_user = User.query.order_by(User.id.asc()).first()
            if ref_user is not None:
                _lo = float(ref_user.cfg_optimal_band_f_min or 65.0)
                _hi = float(ref_user.cfg_optimal_band_f_max or 68.0)
                _mid_f = (_lo + _hi) / 2.0
                sleep_target_c = round((_mid_f - 32.0) * (5.0 / 9.0), 2)
                optimal_band_c_min = round((_lo - 32.0) * (5.0 / 9.0), 4)
                optimal_band_c_max = round((_hi - 32.0) * (5.0 / 9.0), 4)
            else:
                sleep_target_c = 19.5
                optimal_band_c_min = round((65.0 - 32.0) * (5.0 / 9.0), 4)
                optimal_band_c_max = round((68.0 - 32.0) * (5.0 / 9.0), 4)
            drift_cool_margin = 0.5

            with sim_room_lock:
                dev_locked = simulated_room_state.get("dev_simulation") or {}
                temp_display = temp
                voc_display = voc
                if dev_locked.get("force_low_temperature") is True:
                    if ref_user is not None:
                        g_min_f = float(
                            ref_user.cfg_guardrail_temp_f_min or 60.0
                        )
                        g_min_c = (g_min_f - 32.0) * (5.0 / 9.0)
                        temp_display = round(g_min_c - 2.0, 2)
                    else:
                        temp_display = 12.0
                if dev_locked.get("force_voc_spike") is True:
                    base_voc = float(voc) if voc is not None else 0.0
                    voc_display = max(base_voc, 950.0)

                simulated_room_state["environment"] = {
                    "temperature_c": temp_display,
                    "humidity_percent": humid,
                    "ambient_noise_db": noise,
                    "voc_index": voc_display,
                }

                simulated_room_state["sleep_tracking"] = dict(sleep_snapshot)
                simulated_room_state["current_user_state"] = consciousness
                simulated_room_state["sleep_mode_active"] = asleep
                simulated_room_state["sleep_onset_confirmed"] = asleep

                simulated_room_state["sleep_signal_score"] = sleep_signal_score

                baselines = simulated_room_state["baselines"]
                if (
                    baselines.get("ambient_noise_db") is None
                    and noise is not None
                ):
                    baselines["ambient_noise_db"] = noise
                if baselines.get("voc_index") is None and voc is not None:
                    baselines["voc_index"] = voc

                if hr is not None and 35.0 <= float(hr) <= 180.0:
                    bh = baselines.get("heart_rate_bpm")
                    if bh is None:
                        baselines["heart_rate_bpm"] = float(hr)
                    else:
                        baselines["heart_rate_bpm"] = round(
                            0.88 * float(bh) + 0.12 * float(hr),
                            2,
                        )

                drift_detected = []
                temp_base = baselines["temperature_c"]
                hum_base = baselines["humidity_percent"]
                noise_base = baselines["ambient_noise_db"]
                voc_base = baselines["voc_index"]

                temp_effective = temp_display
                if temp_effective is not None:
                    te = float(temp_effective)
                    temp_above_optimal = te > optimal_band_c_max
                    temp_below_optimal = te < optimal_band_c_min
                else:
                    temp_above_optimal = False
                    temp_below_optimal = False

                if temp_above_optimal:
                    drift_detected.append("temperature_above_optimal")
                if temp_below_optimal:
                    drift_detected.append("temperature_below_optimal")
                if (
                    temp is not None
                    and temp_base is not None
                    and abs(temp - temp_base) > 1.5
                ):
                    drift_detected.append("temperature_baseline_drift")
                if (
                    humid is not None
                    and hum_base is not None
                    and abs(humid - hum_base) > 7.0
                ):
                    drift_detected.append("humidity")

                prev_n = simulated_room_state.get("_noise_prev_db")
                noise_spike = False
                noise_constant_high = False
                if noise is not None:
                    n = float(noise)
                    if (
                        prev_n is not None
                        and (n - float(prev_n)) >= config.NOISE_SPIKE_DELTA_DB
                    ):
                        noise_spike = True
                    if noise_base is not None:
                        nb = float(noise_base)
                        if n >= nb + config.NOISE_SPIKE_ABOVE_BASELINE_DB:
                            noise_spike = True
                        elevated = (
                            n >= nb + config.NOISE_SUSTAINED_ABOVE_BASELINE_DB
                            or n >= config.NOISE_HIGH_ABSOLUTE_DB
                        )
                        if elevated:
                            streak = (
                                int(
                                    simulated_room_state.get(
                                        "_noise_high_streak", 0
                                    )
                                )
                                + 1
                            )
                        else:
                            streak = 0
                        simulated_room_state["_noise_high_streak"] = streak
                        noise_constant_high = (
                            streak >= config.NOISE_SUSTAINED_STREAK_TICKS
                            or n >= config.NOISE_HIGH_ABSOLUTE_DB
                        )
                    else:
                        simulated_room_state["_noise_high_streak"] = 0
                        noise_constant_high = (
                            n >= config.NOISE_HIGH_ABSOLUTE_DB
                        )
                else:
                    simulated_room_state["_noise_high_streak"] = 0

                if noise_spike:
                    drift_detected.append("noise_spike")
                if noise_constant_high:
                    drift_detected.append("noise_sustained_high")

                # VOC hazard logic (runs regardless of ASLEEP state).
                voc_abs_alarm = (
                    voc is not None
                    and float(voc) > config.MQ135_SAFE_INDEX_MAX
                )
                voc_rel_alarm = (
                    voc is not None
                    and voc_base is not None
                    and float(voc_base) > 0
                    and float(voc) > float(voc_base) * 1.25
                )
                voc_alarm_active = voc_abs_alarm or voc_rel_alarm

                session_pk = sleep_snapshot.get("sleep_session_id")
                session_hr_mean = session_mean_heart_rate_bpm(
                    session_pk if asleep else None,
                )
                # If HR is high and room is too warm, cool harder.
                corr_rule_a_high_hr_hot = (
                    asleep
                    and hr is not None
                    and session_hr_mean is not None
                    and float(session_hr_mean) > 0
                    and float(hr) > float(session_hr_mean) * 1.10
                    and temp_above_optimal
                )
                simulated_room_state["_autonomy_hints"] = {
                    "voc_drift_active": voc_alarm_active,
                    "biological_cooling_high_hr_hot": corr_rule_a_high_hr_hot,
                }

                simulated_room_state["_hw_inputs"] = {
                    "temp_above_optimal": temp_above_optimal,
                    "temp_below_optimal": temp_below_optimal,
                    "noise_spike": noise_spike,
                    "noise_constant_high": noise_constant_high,
                }

                ma_ctx = simulated_room_state.setdefault(
                    "_micro_arousal_ctx",
                    default_micro_arousal_ctx(),
                )
                micro_evt = micro_arousal_tick(
                    ma_ctx,
                    now_utc=get_current_utc_time(),
                    heart_rate_bpm=hr,
                    ambient_noise_db=noise,
                    noise_baseline_db=noise_base,
                )
                if micro_evt is not None:
                    db.session.add(micro_evt)
                    db.session.commit()
                    append_room_change(
                        "micro_arousal",
                        micro_evt.disruption_label,
                        {
                            "spike_noise_db": micro_evt.spike_noise_db,
                            "prv_median_before_ms": (
                                micro_evt.prv_median_before_ms
                            ),
                            "prv_observed_ms": micro_evt.prv_observed_ms,
                            "prv_drop_ms": micro_evt.prv_drop_ms,
                        },
                    )

                interventions_ok = asleep

                prev_voc_evt = simulated_room_state.get(
                    "_voc_boost_event_prev", False
                )
                if voc_alarm_active and not prev_voc_evt:
                    append_room_change(
                        "voc_ventilation_boost",
                        (
                            "VOC / air-quality hazard; air filtration engaged "
                            "(independent of sleep state)"
                        ),
                        {
                            "voc_live": voc,
                            "voc_baseline": voc_base,
                            "mq135_safe_ceiling": config.MQ135_SAFE_INDEX_MAX,
                            "absolute_trigger": voc_abs_alarm,
                            "relative_trigger": voc_rel_alarm,
                        },
                    )
                simulated_room_state["_voc_boost_event_prev"] = (
                    voc_alarm_active
                )

                if interventions_ok:
                    simulated_room_state["state"] = "consciousness_sleep"
                    simulated_room_state["occupied"] = True
                    simulated_room_state["lighting"] = {
                        "state": "dimmed",
                        "brightness_percent": 6,
                        "lumens_nominal": 2,
                        "lumens_range_lm": {"min": 0, "max": 10},
                    }
                    base_hvac_sleep = {
                        "mode": "cooling",
                        "target_temperature_c": sleep_target_c,
                    }
                    simulated_room_state["hvac"] = dict(base_hvac_sleep)
                    if drift_detected:
                        simulated_room_state["ventilation"] = {
                            "mode": "boost",
                            "fan_percent": 80,
                        }
                        if "temperature_above_optimal" in drift_detected:
                            simulated_room_state["hvac"] = {
                                "mode": "cooling",
                                "target_temperature_c": round(
                                    sleep_target_c - drift_cool_margin,
                                    2,
                                ),
                            }
                        elif "temperature_below_optimal" in drift_detected:
                            simulated_room_state["hvac"] = {
                                "mode": "heating",
                                "target_temperature_c": round(
                                    sleep_target_c + drift_cool_margin,
                                    2,
                                ),
                            }
                        append_room_change(
                            "environmental_drift",
                            (
                                "baseline drift interventions "
                                "(armed only during ASLEEP)"
                            ),
                            {
                                "drift_metrics": drift_detected,
                                "consciousness_state": consciousness,
                                "current_environment": simulated_room_state[
                                    "environment"
                                ],
                            },
                        )
                    else:
                        simulated_room_state["ventilation"] = {
                            "mode": "normal",
                            "fan_percent": 25,
                        }

                else:
                    simulated_room_state["state"] = "occupied_monitoring"
                    simulated_room_state["occupied"] = True
                    simulated_room_state["lighting"] = {
                        "state": "ambient",
                        "brightness_percent": 8,
                        "lumens_nominal": 5,
                        "lumens_range_lm": {"min": 0, "max": 10},
                    }
                    simulated_room_state["hvac"] = {
                        "mode": "circulating",
                        "target_temperature_c": 17.0,
                    }
                    simulated_room_state["ventilation"] = {
                        "mode": "normal",
                        "fan_percent": 25,
                    }

                # VOC response always takes priority.
                if voc_alarm_active:
                    simulated_room_state["ventilation"] = {
                        "mode": "boost",
                        "fan_percent": 95,
                        "reason": "voc_air_quality_exception",
                    }

                simulated_room_state["last_drift_detected"] = list(
                    drift_detected
                )
                if noise is not None:
                    simulated_room_state["_noise_prev_db"] = float(noise)

                refresh_simulated_hardware_locked()


def start_background_tasks():
    global \
        monitor_thread_started, \
        readiness_thread_started, \
        optimal_band_thread_started
    if not monitor_thread_started:
        worker = threading.Thread(
            target=evaluate_sleep_and_environment,
            name="sleep-environment-monitor",
            daemon=True,
        )
        worker.start()
        monitor_thread_started = True

    if not readiness_thread_started:
        readiness_worker = threading.Thread(
            target=calculate_sleep_readiness_worker,
            name="sleep-readiness-calculator",
            daemon=True,
        )
        readiness_worker.start()
        readiness_thread_started = True

    if not optimal_band_thread_started:
        band_worker = threading.Thread(
            target=update_optimal_band_worker,
            name="optimal-band-daily-update",
            daemon=True,
        )
        band_worker.start()
        optimal_band_thread_started = True
