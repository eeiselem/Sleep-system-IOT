"""JSON API routes (dashboard + room sim clients)."""
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from openai import OpenAI
from flask import Blueprint, current_app, request
from flask_login import current_user, login_required
from pydantic import ValidationError

from crud import reading
from db import db
from live_readings import build_latest_live_readings_payload
from logic import get_sleep_session_resolution_context, update_optimal_band
from models import SubjectiveSleepReviewIn
import room_sim
from room_sim import (
    append_room_change,
    compute_sunrise_sequence,
    refresh_simulated_hardware_locked,
    sim_room_lock,
    simulated_room_state,
)
from schemas.reading import Reading
from schemas.sleep_session import SleepSession
from schemas.subjective_sleep_review import SubjectiveSleepReview
from sleep_api import (
    _bind_subjective_review_to_ground_truth,
    _format_sleep_session_display_label,
    _session_sensor_nightly_averages,
    build_sleep_night_points,
    readings_context_anonymized_for_llm,
    resolve_sleep_session_for_night_chart,
    serialize_subjective_sleep_review,
    serialize_subjective_sleep_review_history_entry,
    subjective_review_target_date,
    _wake_calendar_date,
)
from timefmt import to_utc_datetime, utc_isoformat_z
from utils import get_current_utc_time

bp = Blueprint("api", __name__, url_prefix="/api")

@bp.route('/user-config', methods=['GET', 'POST'])
@login_required
def user_config():
    allowed_wake_days = {"daily", "weekdays", "weekends"}

    if request.method == 'GET':
        return {
            "wake_time": current_user.cfg_wake_time,
            "wake_days": current_user.cfg_wake_days or "daily",
            "guardrail_temp_f_min": (
                current_user.cfg_guardrail_temp_f_min
                if current_user.cfg_guardrail_temp_f_min is not None
                else 60.0
            ),
            "guardrail_temp_f_max": (
                current_user.cfg_guardrail_temp_f_max
                if current_user.cfg_guardrail_temp_f_max is not None
                else 75.0
            ),
            "optimal_band_f_min": (
                current_user.cfg_optimal_band_f_min
                if current_user.cfg_optimal_band_f_min is not None
                else 65.0
            ),
            "optimal_band_f_max": (
                current_user.cfg_optimal_band_f_max
                if current_user.cfg_optimal_band_f_max is not None
                else 68.0
            ),
            "override_optimal_band": bool(current_user.cfg_override_optimal_band),
        }, 200

    payload = request.get_json(silent=True) or {}

    def parse_optional_float(value):
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("numeric fields must be valid numbers")

    guardrail_f_min = guardrail_f_max = optimal_f_min = optimal_f_max = None
    try:
        if "wake_time" in payload:
            wake_val = payload.get("wake_time")
            if wake_val in ("", None):
                current_user.cfg_wake_time = None
            else:
                if (
                    not isinstance(wake_val, str)
                    or len(wake_val) != 5
                    or wake_val[2] != ":"
                ):
                    return {"error": "wake_time must be in HH:MM format"}, 400
                hh, mm = wake_val.split(":")
                if not (hh.isdigit() and mm.isdigit()):
                    return {"error": "wake_time must be in HH:MM format"}, 400
                if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                    return {"error": "wake_time must be in HH:MM format"}, 400
                current_user.cfg_wake_time = wake_val

        if "wake_days" in payload:
            wd = payload.get("wake_days") or "daily"
            if wd not in allowed_wake_days:
                return {
                    "error": "wake_days must be daily, weekdays, or weekends",
                }, 400
            current_user.cfg_wake_days = wd

        has_guardrail_min = "guardrail_temp_f_min" in payload
        has_guardrail_max = "guardrail_temp_f_max" in payload
        if has_guardrail_min:
            guardrail_f_min = parse_optional_float(
                payload.get("guardrail_temp_f_min")
            )
            if guardrail_f_min is None:
                return {"error": "guardrail_temp_f_min invalid"}, 400
            current_user.cfg_guardrail_temp_f_min = guardrail_f_min
        if has_guardrail_max:
            guardrail_f_max = parse_optional_float(
                payload.get("guardrail_temp_f_max")
            )
            if guardrail_f_max is None:
                return {"error": "guardrail_temp_f_max invalid"}, 400
            current_user.cfg_guardrail_temp_f_max = guardrail_f_max

        eff_g_min = (
            current_user.cfg_guardrail_temp_f_min or 60.0
        )
        eff_g_max = (
            current_user.cfg_guardrail_temp_f_max or 75.0
        )
        if eff_g_min >= eff_g_max:
            return {"error": "guardrail_temp_f_min must be below max"}, 400
        if eff_g_min < 40 or eff_g_max > 110:
            return {
                "error": "guardrail range must be within 40°F and 110°F",
            }, 400

        has_ob_min = "optimal_band_f_min" in payload
        has_ob_max = "optimal_band_f_max" in payload
        if has_ob_min != has_ob_max:
            return {
                "error": "send both optimal_band_f_min and optimal_band_f_max",
            }, 400
        if has_ob_min:
            optimal_f_min = parse_optional_float(
                payload.get("optimal_band_f_min")
            )
            optimal_f_max = parse_optional_float(
                payload.get("optimal_band_f_max")
            )
            if optimal_f_min is None or optimal_f_max is None:
                return {"error": "optimal band values invalid"}, 400
            if optimal_f_min >= optimal_f_max:
                return {"error": "optimal_band_f_min must be below max"}, 400
            if optimal_f_min < eff_g_min or optimal_f_max > eff_g_max:
                return {
                    "error": "optimal band must lie within absolute guardrails",
                }, 400
            current_user.cfg_optimal_band_f_min = optimal_f_min
            current_user.cfg_optimal_band_f_max = optimal_f_max

        if "override_optimal_band" in payload:
            override_optimal = payload.get("override_optimal_band")
            if not isinstance(override_optimal, bool):
                return {
                    "error": "override_optimal_band must be boolean",
                }, 400
            current_user.cfg_override_optimal_band = override_optimal
    except ValueError as err:
        return {"error": str(err)}, 400
    db.session.commit()

    return {
        "status": "saved",
        "config": {
            "wake_time": current_user.cfg_wake_time,
            "wake_days": current_user.cfg_wake_days,
            "guardrail_temp_f_min": (
                current_user.cfg_guardrail_temp_f_min or 60.0
            ),
            "guardrail_temp_f_max": (
                current_user.cfg_guardrail_temp_f_max or 75.0
            ),
            "optimal_band_f_min": (
                current_user.cfg_optimal_band_f_min or 65.0
            ),
            "optimal_band_f_max": (
                current_user.cfg_optimal_band_f_max or 68.0
            ),
            "override_optimal_band": bool(
                current_user.cfg_override_optimal_band
            ),
        },
    }, 200


@bp.route('/subjective-sleep-review/status', methods=['GET'])
@login_required
def subjective_sleep_review_status():
    now_utc = get_current_utc_time()
    target = subjective_review_target_date(now_utc)
    row = SubjectiveSleepReview.query.filter_by(
        user_id=current_user.id,
        feedback_for_date=target,
    ).first()
    return {
        "target_session_date": target.isoformat(),
        "has_review": row is not None,
        "review": serialize_subjective_sleep_review(row),
    }, 200


@bp.route('/subjective-sleep-review', methods=['POST'])
@login_required
def subjective_sleep_review_save():
    try:
        payload = SubjectiveSleepReviewIn.model_validate(
            request.get_json(silent=True) or {}
        )
    except ValidationError as exc:
        return {"error": "Invalid request body", "details": exc.errors()}, 400

    now_utc = get_current_utc_time()
    session_date = payload.session_date or subjective_review_target_date(now_utc)
    notes_str = (
        payload.notes.strip()
        if payload.notes is not None and payload.notes.strip()
        else None
    )

    row = SubjectiveSleepReview.query.filter_by(
        user_id=current_user.id,
        feedback_for_date=session_date,
    ).first()
    if row:
        row.rating = payload.rating
        row.notes = notes_str
        row.created_at = get_current_utc_time()
    else:
        row = SubjectiveSleepReview(
            user_id=current_user.id,
            feedback_for_date=session_date,
            rating=payload.rating,
            notes=notes_str,
        )
        db.session.add(row)

    discrepancy_logged = _bind_subjective_review_to_ground_truth(
        row,
        current_user.id,
        session_date,
        payload.rating,
    )

    db.session.commit()
    saved = SubjectiveSleepReview.query.filter_by(
        user_id=current_user.id,
        feedback_for_date=session_date,
    ).first()
    try:
        update_optimal_band(current_user, session_date)
    except Exception:
        current_app.logger.exception(
            "update_optimal_band failed after subjective review user=%s date=%s",
            current_user.id,
            session_date.isoformat(),
        )

    out: Dict[str, Any] = {
        "status": "saved",
        "review": serialize_subjective_sleep_review(saved),
    }
    if discrepancy_logged:
        out["discrepancy_logged"] = True
    return out, 200


@bp.route('/subjective-sleep-review/history', methods=['GET'])
@login_required
def subjective_sleep_review_history():
    limit_raw = request.args.get("limit", "40")
    try:
        limit = int(limit_raw)
    except ValueError:
        return {"error": "limit must be a positive integer"}, 400
    if limit <= 0:
        return {"error": "limit must be a positive integer"}, 400
    limit = min(limit, 120)

    days_raw = request.args.get("days")
    start_cutoff = None
    if days_raw is not None and str(days_raw).strip() != "":
        try:
            days_win = int(days_raw)
        except ValueError:
            return {"error": "days must be a positive integer"}, 400
        if days_win < 1 or days_win > 90:
            return {"error": "days must be between 1 and 90"}, 400
        start_cutoff = get_current_utc_time().date() - timedelta(days=days_win - 1)

    q = SubjectiveSleepReview.query.filter_by(user_id=current_user.id)
    if start_cutoff is not None:
        q = q.filter(SubjectiveSleepReview.feedback_for_date >= start_cutoff)

    rows = (
        q.order_by(SubjectiveSleepReview.feedback_for_date.desc())
        .limit(limit)
        .all()
    )
    return {
        "days_window": days_raw,
        "count": len(rows),
        "reviews": [serialize_subjective_sleep_review_history_entry(r) for r in rows],
    }, 200




# called by JavaScript on the dashboard every 30 seconds to get the newest reading without refreshing the page.
@bp.route('/latest-readings')
@login_required
def latest_readings():
    # updates the "Current Readings" box — merges recent rows so a biometric-only POST
    # does not replace environment fields with placeholder zeros.
    return build_latest_live_readings_payload(current_user.id), 200


@bp.route("/sleep-coach", methods=["POST"])
@login_required
def api_sleep_coach():
    payload = request.get_json(silent=True) or {}
    user_q = (
        payload.get("query")
        or payload.get("q")
        or payload.get("message")
        or ""
    )
    user_q = str(user_q).strip()
    if not user_q:
        msg = (
            'JSON body must include text, e.g. {"query": "How can I sleep better?"}'
        )
        return {"error": msg}, 400

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return {"error": "OPENAI_API_KEY is not set"}, 503

    anonymized_bundle = readings_context_anonymized_for_llm(
        days=7, limit=2500, user_id=current_user.id,
    )
    coach_model = (
        (os.getenv("OPENAI_SLEEP_COACH_MODEL") or "gpt-4o-mini").strip()
        or "gpt-4o-mini"
    )
    sys_prompt = (
        "You are the user's premium sleep coach: calm, authoritative, and brief—similar in "
        "tone to the Oura app. You answer using only the anonymized bedroom IoT time series "
        "in the user message below (timestamps and sensor fields: temperature, humidity, air "
        "quality, noise, light, HR, SpO₂, motion/gyro). There are no PII strings. Ground "
        "advice in what those signals imply plus sound sleep physiology.\n\n"
        "Formatting (required): Respond in strict, highly scannable Markdown. Prefer short "
        "bullet lists (one idea per bullet). Use `##` section headers when helpful, each "
        "with a concise title and a fitting emoji—for example 🌬️ Air quality, 🫀 Vitals, "
        "💡 Environment (pick emojis that match the section).\n\n"
        "Tone and content rules:\n"
        "- Never open with robotic meta-phrases such as \"Based on the provided readings\" "
        "or \"Based on the data\"; start like a coach speaking to the user.\n"
        "- Do not apologize, hedge about gaps, or mention missing/absent/unavailable sensors "
        "or values. Silently analyze only what appears in the JSON; skip topics with no usable "
        "values.\n"
        "- Give practical sleep-hygiene and environment tweaks aligned with the data.\n"
        "- Do not invent medical diagnoses or replace a clinician; if something is "
        "concerning, state it succinctly as a reason to seek professional advice."
    )
    serialized_ctx = json.dumps(anonymized_bundle)
    max_ctx_chars = min(340_000, int(os.getenv("SLEEP_COACH_CONTEXT_CHARS", "340000")))
    if len(serialized_ctx) > max_ctx_chars:
        serialized_ctx = serialized_ctx[:max_ctx_chars] + "…truncated-for-token-cap"

    user_block = (
        "Anonymized readings JSON (possibly truncated):\n"
        f"{serialized_ctx}\n\n"
        f"User question:\n{user_q}"
    )

    try:
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=coach_model,
            temperature=0.45,
            max_tokens=900,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_block},
            ],
        )
        recommendation = (
            completion.choices[0].message.content or ""
        ).strip()
        if not recommendation:
            return {"error": "Model returned empty text"}, 502
    except Exception as exc:
        return {"error": f"Upstream LLM failure: {exc}"}, 502

    return {
        "recommendation": recommendation,
        "model": coach_model,
        "samples_in_context": anonymized_bundle["sample_rows_returned"],
    }


def _serialize_sleep_session_score(row):
    if row is None:
        return None
    wake_day = (
        to_utc_datetime(row.ended_at).date().isoformat()
        if row.ended_at is not None
        else to_utc_datetime(row.started_at).date().isoformat()
    )
    return {
        "id": row.id,
        "score_date": wake_day,
        "readiness_score": row.readiness_score,
        "sleep_efficiency_pct": row.sleep_efficiency_pct,
        "sleep_efficiency_score": row.sleep_efficiency_score,
        "avg_prv_ms": row.avg_prv_ms,
        "prv_score": row.prv_score,
        "spo2_drop_count": row.spo2_drop_count,
        "noise_spike_count": row.noise_spike_count,
        "disturbance_score": row.disturbance_score,
        "sample_count": row.sample_count,
        "calculated_at": (
            utc_isoformat_z(row.calculated_at)
            if row.calculated_at is not None else None
        ),
    }


@bp.route('/sleep-readiness/latest')
@login_required
def sleep_readiness_latest():
    latest_score = (
        SleepSession.query.filter(
            SleepSession.user_id == current_user.id,
            SleepSession.ended_at.isnot(None),
            SleepSession.readiness_score.isnot(None),
        )
        .order_by(SleepSession.ended_at.desc())
        .first()
    )
    if not latest_score:
        return {"score": None}, 200
    return {"score": _serialize_sleep_session_score(latest_score)}, 200


@bp.route('/sleep-readiness/history')
@login_required
def sleep_readiness_history():
    limit_raw = request.args.get("limit", "14")
    try:
        limit = int(limit_raw)
    except ValueError:
        return {"error": "limit must be a positive integer"}, 400
    if limit <= 0:
        return {"error": "limit must be a positive integer"}, 400
    limit = min(limit, 90)

    rows = (
        SleepSession.query.filter(
            SleepSession.user_id == current_user.id,
            SleepSession.ended_at.isnot(None),
            SleepSession.readiness_score.isnot(None),
        )
        .order_by(SleepSession.ended_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "count": len(rows),
        "scores": [_serialize_sleep_session_score(r) for r in rows],
    }, 200


@bp.route('/sleep-session/list')
@login_required
def sleep_session_list():
    """Recent FSM-derived sleep sessions for the Single Night picker."""
    consciousness, active_sid = get_sleep_session_resolution_context()
    rows = (
        SleepSession.query.filter(SleepSession.user_id == current_user.id)
        .order_by(SleepSession.started_at.desc())
        .limit(50)
        .all()
    )
    items = []
    for s in rows:
        ongoing = s.ended_at is None
        items.append({
            "id": s.id,
            "wake_date_utc": _wake_calendar_date(s).isoformat(),
            "display_label": _format_sleep_session_display_label(s),
            "started_at": utc_isoformat_z(s.started_at),
            "ended_at": utc_isoformat_z(s.ended_at) if s.ended_at else None,
            "ongoing": ongoing,
            "fsm_active": bool(
                ongoing
                and active_sid is not None
                and s.id == active_sid
                and consciousness == "ASLEEP"
            ),
        })
    return {"sessions": items}, 200


@bp.route('/sleep-session/night-readings')
@login_required
def sleep_session_night_readings():
    sess, param_err = resolve_sleep_session_for_night_chart(
        session_id_param=request.args.get("session_id"),
        night_param=request.args.get("night"),
        viewer_user_id=current_user.id,
    )
    if param_err:
        code = 404 if param_err == "Session not found." else 400
        return {"error": param_err}, code
    if sess is None:
        return {"error": "No sleep sessions on record.", "session_id": None}, 404

    window_start = to_utc_datetime(sess.started_at)
    window_end = sess.ended_at if sess.ended_at else get_current_utc_time()

    readings = reading.read_sleep_session_between(
        window_start,
        window_end,
        user_id=getattr(sess, "user_id", None),
    )
    points = build_sleep_night_points(readings)

    label_date = _wake_calendar_date(sess).isoformat()

    return {
        "session_id": sess.id,
        "night": label_date,
        "session_start_time": utc_isoformat_z(window_start),
        "session_end_time": utc_isoformat_z(window_end),
        "ongoing": sess.ended_at is None,
        "window_start": utc_isoformat_z(window_start),
        "window_end": utc_isoformat_z(window_end),
        "sample_count": len(points),
        "points": points,
        # Hint for clients: chart labels use browser local TZ; instants are always UTC in JSON.
        "timestamps_are_utc": True,
    }, 200


@bp.route('/sleep-readiness/weekly-summary')
@login_required
def sleep_readiness_weekly_summary():
    """
    Trailing seven **completed** sleep sessions (readiness present): sleep score
    (``readiness_score``), stored session PRV, and per-night means of HR, SpO₂,
    HRV (RMSSD), lux, air quality index, noise, and restful efficiency (0–100) over each
    session window. Each row includes ``display_label`` (same wording as the Single
    Night picker). ``weekly_means`` holds the simple mean of each numeric column
    across nights in the window (for dashboard summaries). The dashboard weekly view
    plots these as multi-series night-over-night trends (normalized in the client).
    """
    rows_chrono = (
        SleepSession.query.filter(
            SleepSession.user_id == current_user.id,
            SleepSession.ended_at.isnot(None),
            SleepSession.readiness_score.isnot(None),
        )
        .order_by(SleepSession.ended_at.desc())
        .limit(7)
        .all()
    )
    rows_chrono = list(reversed(rows_chrono))

    days_out = []
    for hit in rows_chrono:
        wd = to_utc_datetime(hit.ended_at).date().isoformat()
        avgs = _session_sensor_nightly_averages(hit)
        days_out.append({
            "score_date": wd,
            "session_id": hit.id,
            "display_label": _format_sleep_session_display_label(hit),
            "readiness_score": round(float(hit.readiness_score), 2),
            "avg_prv_ms": (
                round(float(hit.avg_prv_ms), 2)
                if hit.avg_prv_ms is not None else None
            ),
            **avgs,
        })

    if rows_chrono:
        range_start = to_utc_datetime(rows_chrono[0].ended_at).date().isoformat()
        range_end = (
            to_utc_datetime(rows_chrono[-1].ended_at).date().isoformat()
        )
    else:
        rd = get_current_utc_time().date().isoformat()
        range_start = rd
        range_end = rd

    weekly_mean_keys = (
        "readiness_score",
        "avg_heart_rate_bpm",
        "avg_spo2_pct",
        "avg_hrv_rmssd_ms",
        "avg_ambient_light_lux",
        "avg_air_quality_index",
        "avg_ambient_noise_db",
        "avg_restlessness_score",
        "avg_prv_ms",
    )
    weekly_means: Dict[str, Optional[float]] = {}
    for key in weekly_mean_keys:
        nums: List[float] = []
        for d in days_out:
            v = d.get(key)
            if v is None:
                continue
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                continue
        weekly_means[key] = round(sum(nums) / len(nums), 3) if nums else None

    return {
        "range_start": range_start,
        "range_end": range_end,
        "days": days_out,
        "weekly_means": weekly_means,
        "nights_in_window": len(days_out),
    }, 200


@bp.route('/simulated-room', methods=['GET', 'POST'])
def simulated_room():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        allowed_keys = {
            "cooling",
            "heater",
            "white_noise",
            "fan",
            "air_filtration_high_fan",
        }
        incoming_keys = set(payload.keys())
        invalid_keys = sorted(incoming_keys - allowed_keys)
        if invalid_keys:
            return {
                "error": "Invalid hardware keys",
                "invalid_keys": invalid_keys,
                "allowed_keys": sorted(list(allowed_keys)),
            }, 400

        with sim_room_lock:
            hardware = simulated_room_state.get("simulated_hardware", {})
            updates = {}
            for key in allowed_keys:
                if key in payload:
                    raw_value = payload[key]
                    if not isinstance(raw_value, bool):
                        return {
                            "error": f"'{key}' must be boolean"
                        }, 400
                    hardware[key] = raw_value
                    updates[key] = raw_value
            simulated_room_state["simulated_hardware"] = hardware
            if updates:
                append_room_change(
                    "simulated_hardware_update",
                    "manual state update via API",
                    updates,
                )

            response_payload = dict(simulated_room_state)
            response_payload["timestamp"] = utc_isoformat_z(
                get_current_utc_time()
            )
            response_payload["recent_changes"] = list(
                simulated_room_state["recent_changes"]
            )
        return response_payload, 200

    wake_time = None
    if current_user.is_authenticated:
        wake_time = current_user.cfg_wake_time
    now_utc = get_current_utc_time()
    sunrise = compute_sunrise_sequence(now_utc, wake_time)

    with sim_room_lock:
        response_payload = dict(simulated_room_state)
        lighting_state = dict(simulated_room_state.get("lighting", {}))
        lighting_state["brightness_percent"] = sunrise["brightness_percent"]
        lighting_state["lumen"] = sunrise["lumen"]
        lighting_state["color_temperature_k"] = sunrise["color_temperature_k"]
        response_payload["lighting"] = lighting_state
        response_payload["sunrise_sequence"] = sunrise
        response_payload["timestamp"] = utc_isoformat_z(now_utc)
        response_payload["recent_changes"] = list(
            simulated_room_state["recent_changes"]
        )
    return response_payload, 200


@bp.route('/dev/simulation', methods=['POST'])
@login_required
def dev_simulation_toggle():
    """
    Dashboard-only presentation toggles. Forces intervention badges ACTIVE without real sensor drift.

    POST JSON may include booleans ``force_high_temperature``, ``force_high_noise``,
    ``force_low_temperature``, and ``force_voc_spike`` (partial updates supported),
    or a single ``scenario`` string that sets a coherent profile:

    - ``cold_night`` — low temperature / heater path (clears other forces)
    - ``hot_room`` — high temperature / cooling path
    - ``noisy_room`` — high noise / masking path
    - ``voc_spike`` — VOC / filtration path
    - ``clear`` — all forces off

    ``scenario`` is mutually exclusive with individual flag keys in one request.
    """
    payload = request.get_json(silent=True) or {}
    allowed_flags = frozenset(
        {
            "force_high_temperature",
            "force_high_noise",
            "force_low_temperature",
            "force_voc_spike",
        }
    )
    scenario_profiles = {
        "cold_night": {
            "force_high_temperature": False,
            "force_high_noise": False,
            "force_low_temperature": True,
            "force_voc_spike": False,
        },
        "hot_room": {
            "force_high_temperature": True,
            "force_high_noise": False,
            "force_low_temperature": False,
            "force_voc_spike": False,
        },
        "noisy_room": {
            "force_high_temperature": False,
            "force_high_noise": True,
            "force_low_temperature": False,
            "force_voc_spike": False,
        },
        "voc_spike": {
            "force_high_temperature": False,
            "force_high_noise": False,
            "force_low_temperature": False,
            "force_voc_spike": True,
        },
        "clear": {k: False for k in allowed_flags},
    }

    scenario = payload.get("scenario")
    if scenario is not None:
        if not isinstance(scenario, str) or not scenario.strip():
            return {"error": "'scenario' must be a non-empty string"}, 400
        key = scenario.strip().lower()
        if key not in scenario_profiles:
            return {
                "error": "Unknown scenario",
                "unknown_scenario": scenario,
                "allowed_scenarios": sorted(scenario_profiles.keys()),
            }, 400
        extra = set(payload.keys()) - {"scenario"}
        if extra:
            return {
                "error": "Use either 'scenario' or individual force_* keys, not both",
                "unexpected_keys": sorted(extra),
            }, 400
        with sim_room_lock:
            dev = simulated_room_state.setdefault(
                "dev_simulation",
                {
                    "force_high_temperature": False,
                    "force_high_noise": False,
                    "force_low_temperature": False,
                    "force_voc_spike": False,
                },
            )
            dev.update(scenario_profiles[key])
            refresh_simulated_hardware_locked()
            out = {
                "dev_simulation": dict(dev),
                "simulated_hardware": dict(
                    simulated_room_state.get("simulated_hardware", {})
                ),
                "timestamp": utc_isoformat_z(get_current_utc_time()),
            }
        return out, 200

    incoming = set(payload.keys())
    unsupported = sorted(incoming - allowed_flags)
    if unsupported:
        return {
            "error": "Unknown keys",
            "unknown_keys": unsupported,
            "allowed_keys": sorted(allowed_flags),
        }, 400

    with sim_room_lock:
        dev = simulated_room_state.setdefault(
            "dev_simulation",
            {
                "force_high_temperature": False,
                "force_high_noise": False,
                "force_low_temperature": False,
                "force_voc_spike": False,
            },
        )
        for key in allowed_flags:
            if key not in payload:
                continue
            val = payload[key]
            if not isinstance(val, bool):
                return {"error": f"'{key}' must be a boolean"}, 400
            dev[key] = val

        refresh_simulated_hardware_locked()
        out = {
            "dev_simulation": dict(dev),
            "simulated_hardware": dict(
                simulated_room_state.get("simulated_hardware", {})
            ),
            "timestamp": utc_isoformat_z(get_current_utc_time()),
        }
    return out, 200


@bp.route('/simulated-room/changes')
def simulated_room_changes():
    since_param = request.args.get("since")
    cursor_param = request.args.get("cursor")
    limit_param = request.args.get("limit")
    since_dt = None
    start_index = 0
    limit = 10

    if since_param:
        normalized_since = since_param.replace("Z", "+00:00")
        try:
            since_dt = datetime.fromisoformat(normalized_since)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
            else:
                since_dt = since_dt.astimezone(timezone.utc)
        except ValueError:
            return {
                "error": "Invalid 'since' format. Use ISO-8601 datetime."
            }, 400

    if cursor_param:
        try:
            start_index = int(cursor_param)
            if start_index < 0:
                raise ValueError
        except ValueError:
            return {
                "error": "Invalid 'cursor'. Use a non-negative integer."
            }, 400

    if limit_param:
        try:
            limit = int(limit_param)
            if limit <= 0:
                raise ValueError
            limit = min(limit, 50)
        except ValueError:
            return {
                "error": "Invalid 'limit'. Use a positive integer."
            }, 400

    with sim_room_lock:
        all_changes = list(simulated_room_state["recent_changes"])

    if since_dt is None:
        filtered_changes = all_changes
    else:
        filtered_changes = []
        for event in all_changes:
            event_ts = event.get("timestamp")
            if not event_ts:
                continue
            try:
                event_dt = datetime.fromisoformat(
                    event_ts.replace("Z", "+00:00")
                )
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
                else:
                    event_dt = event_dt.astimezone(timezone.utc)
            except ValueError:
                continue

            if event_dt > since_dt:
                filtered_changes.append(event)

    paged_changes = filtered_changes[start_index:start_index + limit]
    next_index = start_index + len(paged_changes)
    has_more = next_index < len(filtered_changes)

    return {
        "count": len(paged_changes),
        "total_matching": len(filtered_changes),
        "cursor": str(start_index),
        "next_cursor": str(next_index) if has_more else None,
        "has_more": has_more,
        "changes": paged_changes,
    }, 200

