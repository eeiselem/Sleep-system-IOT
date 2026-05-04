from flask import Blueprint, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from config import EVENT_LOG_BREACH_LABELS, EVENT_LOG_BREACH_METRICS
from crud import reading
from live_readings import build_latest_live_readings_payload
from timefmt import parse_local_datetime_to_utc
from utils import format_temperature_fahrenheit_display

bp = Blueprint("dashboard", __name__)


def _pop_event_log_query_keys_only():
    session.pop("event_metric", None)
    session.pop("event_threshold", None)
    session.pop("event_direction", None)
    session.pop("start_datetime", None)
    session.pop("end_datetime", None)
    session.pop("threshold_temperature", None)
    session.pop("direction", None)


def _pop_event_log_session_keys():
    _pop_event_log_query_keys_only()
    session.pop("event_log_error", None)


@bp.route("/dashboard")
@login_required
def index():
    live = build_latest_live_readings_payload(current_user.id)

    if getattr(current_user, "role", None) != "Admin":
        _pop_event_log_query_keys_only()

    start_datetime_str = session.get("start_datetime") or ""
    end_datetime_str = session.get("end_datetime") or ""
    event_metric = (session.get("event_metric") or "").strip()
    if event_metric and event_metric not in EVENT_LOG_BREACH_METRICS:
        session.pop("event_metric", None)
        session.pop("event_threshold", None)
        event_metric = ""
    event_threshold_raw = session.get("event_threshold")
    event_direction = session.get("event_direction") or "below"

    search_results = None
    event_log_summary = None
    event_metric_highlight = ""
    breach_table_rows = None
    event_log_error = session.pop("event_log_error", None)

    def _has_valid_breach_query():
        if event_metric not in EVENT_LOG_BREACH_METRICS:
            return False
        if event_threshold_raw in (None, "") or not str(event_threshold_raw).strip():
            return False
        return True

    if current_user.role == "Admin" and _has_valid_breach_query():
        start_date = parse_local_datetime_to_utc(start_datetime_str or None)
        end_date = parse_local_datetime_to_utc(end_datetime_str or None)

        thr_clean = str(event_threshold_raw).strip()
        direction = event_direction if event_direction in ("above", "below") else "below"

        search_rows = reading.read_search(
            metric=event_metric,
            threshold=thr_clean,
            direction=direction,
            start_date=start_date,
            end_date=end_date,
            threshold_temperature=None,
        )
        lbl = EVENT_LOG_BREACH_LABELS[event_metric]
        event_log_summary = (
            f"{lbl}: values {direction} threshold {thr_clean} "
            f"(optional local window applied; default scope: last 7 calendar days)"
        )
        event_metric_highlight = event_metric
        search_results = search_rows if search_rows is not None else []
        breach_table_rows = []
        for r in search_results:
            raw_val = getattr(r, event_metric, None)
            display_val = raw_val
            if event_metric == "temperature" and raw_val is not None:
                f_str = format_temperature_fahrenheit_display(raw_val)
                display_val = (
                    f"{f_str}°F"
                    if f_str not in ("-", "Encrypting…")
                    else f_str
                )
            breach_table_rows.append(
                {
                    "id": r.id,
                    "timestamp": r.timestamp,
                    "value": display_val,
                }
            )

    return render_template(
        "dashboard.html",
        user=current_user,
        live_readings_initial=live,
        current_temperature=live.get("temperature"),
        current_humidity=live.get("humidity"),
        current_heart_rate=live.get("heart_rate"),
        current_spo2=live.get("spo2"),
        current_ambient_noise=live.get("ambient_noise"),
        current_ambient_light=live.get("ambient_light"),
        current_voc=live.get("air_quality"),
        current_restlessness_score=live.get("restlessness_score"),
        search_results=search_results,
        breach_table_rows=breach_table_rows,
        event_log_summary=event_log_summary,
        event_log_breach_labels=EVENT_LOG_BREACH_LABELS,
        event_metric_highlight=event_metric_highlight,
        event_log_error=event_log_error,
    )


@bp.route("/submit-event-log-search", methods=["POST"])
@login_required
def event_log_search():
    if getattr(current_user, "role", None) != "Admin":
        return redirect(url_for("dashboard.index"))
    metric = (request.form.get("metric") or "").strip()
    threshold = request.form.get("threshold")
    direction = (request.form.get("direction") or "below").strip().lower()
    start = request.form.get("start_time") or ""
    end = request.form.get("end_time") or ""

    def _reject(msg: str):
        session["event_log_error"] = msg
        session["event_metric"] = metric
        session["event_threshold"] = threshold or ""
        session["event_direction"] = direction if direction in ("above", "below") else "below"
        session["start_datetime"] = start
        session["end_datetime"] = end
        return redirect(url_for("dashboard.index"))

    if metric not in EVENT_LOG_BREACH_METRICS:
        return _reject(
            "Select one metric: heart rate, SpO₂, ambient noise, air quality index, or raw motion column (gyro / activity)."
        )
    if threshold in (None, "") or not str(threshold).strip():
        return _reject("Enter a numeric threshold.")
    try:
        float(str(threshold).strip())
    except ValueError:
        return _reject("Threshold must be a valid number.")

    if direction not in ("above", "below"):
        direction = "below"

    session.pop("threshold_temperature", None)
    session.pop("direction", None)
    session.pop("event_log_error", None)

    session["event_metric"] = metric
    session["event_threshold"] = str(threshold).strip()
    session["event_direction"] = direction
    session["start_datetime"] = start
    session["end_datetime"] = end

    return redirect(url_for("dashboard.index"))


@bp.route("/submit-temperature-search", methods=["POST"])
@login_required
def temperature_search():
    if getattr(current_user, "role", None) != "Admin":
        return redirect(url_for("dashboard.index"))
    thresh = request.form.get("threshold_temperature")
    direction = request.form.get("direction") or "above"
    start = request.form.get("start_time") or ""
    end = request.form.get("end_time") or ""
    session["event_metric"] = ""
    session["event_threshold"] = thresh
    session["event_direction"] = direction
    session["threshold_temperature"] = thresh
    session["direction"] = direction
    session["start_datetime"] = start
    session["end_datetime"] = end
    return redirect(url_for("dashboard.index"))


@bp.route("/clear-event-log-search", methods=["POST"])
@login_required
def clear_event_log_search():
    if getattr(current_user, "role", None) != "Admin":
        return redirect(url_for("dashboard.index"))
    _pop_event_log_session_keys()
    return redirect(url_for("dashboard.index"))


@bp.route("/clear-temperature-search", methods=["POST"])
@login_required
def clear_search():
    if getattr(current_user, "role", None) != "Admin":
        return redirect(url_for("dashboard.index"))
    _pop_event_log_session_keys()
    return redirect(url_for("dashboard.index"))
