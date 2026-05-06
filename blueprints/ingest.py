import hashlib
import os
import secrets
from functools import wraps

"""Ingest endpoints for ESP32 device payloads.

This blueprint validates incoming payloads, checks API keys,
stores normalized readings, and then triggers state updates.
"""

from flask import Blueprint, current_app, request
from pydantic import ValidationError

import room_sim

from crud import reading
from logic import default_ingest_user_id, evaluate_sleep_state
from models import BiometricPayload, EnvironmentReadingIn, ReadingBase
from utils import decrypt_biometric_aes128_cbc_b64, ingest_field_plaintext

bp = Blueprint("ingest", __name__)


def _digest_ingest_key(value: str) -> bytes:
    # Hash key material before compare to avoid leaking timing on raw strings.
    return hashlib.sha256((value or "").encode("utf-8")).digest()


def _require_ingest_api_key():
    # Common auth gate for all ingest endpoints.
    expected_key = (os.getenv("INGEST_API_KEY") or "").strip()
    if not expected_key:
        return {
            "error": "Server misconfiguration: INGEST_API_KEY is not set",
        }, 503

    supplied = (request.headers.get("X-API-KEY") or "").strip()
    if not secrets.compare_digest(
        _digest_ingest_key(supplied),
        _digest_ingest_key(expected_key),
    ):
        return {"error": "Unauthorized"}, 401
    return None


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        rejected = _require_ingest_api_key()
        if rejected is not None:
            return rejected
        return fn(*args, **kwargs)

    return wrapper


def _json_object_body():
    # Enforce JSON object body (not array/string/null).
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, ({"error": "JSON object body required"}, 400)
    return payload, None


def _cache_bump():
    # Keep sim cache count in sync when new readings land.
    if room_sim.total_records_cache is not None:
        room_sim.total_records_cache += 1


def _run_post_ingest_state_update(log_context: str) -> None:
    # Ingest should not fail hard if state refresh throws.
    try:
        evaluate_sleep_state(current_app)
    except Exception:
        current_app.logger.exception(log_context)


def persist_reading_base(clean_data: ReadingBase) -> None:
    # Save full reading row, then update sleep state.
    reading.create(
        temperature=ingest_field_plaintext(clean_data.temperature),
        humidity=ingest_field_plaintext(clean_data.humidity),
        air_quality=ingest_field_plaintext(clean_data.air_quality),
        ambient_noise=ingest_field_plaintext(clean_data.ambient_noise),
        ambient_light=ingest_field_plaintext(clean_data.ambient_light),
        heart_rate=ingest_field_plaintext(clean_data.heart_rate),
        spo2=ingest_field_plaintext(clean_data.spo2),
        gyro_variance=ingest_field_plaintext(clean_data.gyro_variance),
        user_id=default_ingest_user_id(),
    )
    _run_post_ingest_state_update("evaluate_sleep_state failed after ingest")
    _cache_bump()


@bp.route("/post-data", methods=["POST"])
@require_api_key
def receive_data():
    # Old combined endpoint (env node + biometric in one row).
    payload, err = _json_object_body()
    if err is not None:
        return err

    try:
        clean_data = ReadingBase(**payload)
        persist_reading_base(clean_data)
        return {"status": "success"}, 200
    except ValidationError as e:
        return {
            "error": "Invalid /post-data payload.",
            "details": e.errors(),
        }, 400
    except Exception as e:
        return {"error": str(e)}, 500


@bp.route("/post-environment", methods=["POST"])
@require_api_key
def receive_environment():
    # Env node endpoint.
    payload, err = _json_object_body()
    if err is not None:
        return err

    try:
        clean_data = EnvironmentReadingIn(**payload)
        reading.create(
            temperature=ingest_field_plaintext(clean_data.temperature),
            humidity=ingest_field_plaintext(clean_data.humidity),
            air_quality=ingest_field_plaintext(clean_data.air_quality),
            ambient_noise=ingest_field_plaintext(clean_data.ambient_noise),
            ambient_light=ingest_field_plaintext(clean_data.ambient_light),
            heart_rate=None,
            spo2=None,
            gyro_variance=ingest_field_plaintext(clean_data.gyro_variance),
            hrv_rmssd=None,
            user_id=default_ingest_user_id(),
        )
        _run_post_ingest_state_update(
            "evaluate_sleep_state failed after environment ingest"
        )
        _cache_bump()

        return {"status": "success"}, 200
    except ValidationError as e:
        return {
            "error": "Invalid /post-environment payload.",
            "details": e.errors(),
        }, 400
    except Exception as e:
        return {"error": str(e)}, 500


@bp.route("/biometric", methods=["POST"])
@require_api_key
def receive_biometric():
    # Biometric node sends encrypted text, not JSON.
    body = (request.get_data(as_text=True) or "").strip()
    obj, dec_err = decrypt_biometric_aes128_cbc_b64(body)
    if dec_err:
        return {"error": dec_err}, 400

    try:
        clean = BiometricPayload(**obj)
    except ValidationError as e:
        return {
            "error": "Invalid biometric payload",
            "details": e.errors(),
        }, 400

    try:
        reading.create(
            temperature=None,
            humidity=None,
            air_quality=None,
            ambient_noise=None,
            ambient_light=None,
            heart_rate=ingest_field_plaintext(clean.heart_rate),
            spo2=ingest_field_plaintext(clean.spo2),
            gyro_variance=ingest_field_plaintext(clean.gyro),
            hrv_rmssd=(
                ingest_field_plaintext(clean.hrv)
                if clean.hrv is not None
                else None
            ),
            user_id=default_ingest_user_id(),
        )
        _run_post_ingest_state_update(
            "evaluate_sleep_state failed after biometric ingest"
        )
        _cache_bump()

        return {"status": "success"}, 200
    except Exception as e:
        return {"error": str(e)}, 500
