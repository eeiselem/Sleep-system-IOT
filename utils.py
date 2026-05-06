import base64
import json
import math
import os
import hashlib
from typing import Any, Optional, Tuple
from datetime import datetime, timezone
from dotenv import load_dotenv
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

"""Crypto, parsing, and formatting helpers shared by backend modules."""

# load values from .env
load_dotenv()

# derive 32-byte AES key from env secret
_master_raw = os.getenv("MASTER_ENCRYPTION_KEY") or ""
master_key = _master_raw.encode("utf-8")
aes_256_key = hashlib.sha256(master_key).digest()


def get_current_utc_time():
    # Single source for UTC "now" so tests and call sites stay consistent.
    return datetime.now(timezone.utc)


def to_float_or_none(value: Any) -> Optional[float]:
    # Best-effort numeric parse; return None on invalid values.
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def mean_or_none(values: list[Optional[float]]) -> Optional[float]:
    # Mean over non-null values only.
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def try_aes256_gcm_decrypt_b64(blob_b64: str) -> Optional[str]:
    # base64(nonce12 + tag16 + ciphertext) -> plaintext or None
    if not blob_b64 or blob_b64 == "N/A":
        return None
    try:
        encrypted_blob = base64.b64decode(blob_b64.strip())
        if len(encrypted_blob) < 28:
            return None
        nonce = encrypted_blob[:12]
        tag = encrypted_blob[12:28]
        ciphertext = encrypted_blob[28:]
        cipher = AES.new(aes_256_key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext.decode("utf-8")
    except Exception:
        return None


def encrypt_at_rest(data: str) -> str:
    # Encrypt plaintext field to base64(nonce+tag+ciphertext) for DB storage.
    if data is None or data == "N/A":
        return data

    plaintext = str(data).encode("utf-8")
    nonce = get_random_bytes(12)
    cipher = AES.new(aes_256_key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    encrypted_blob = nonce + tag + ciphertext
    return base64.b64encode(encrypted_blob).decode("utf-8")


def decrypt_at_rest(data: str) -> str:
    # strict decrypt helper for DB values
    if not data or data == "N/A":
        return data
    pt = try_aes256_gcm_decrypt_b64(str(data))
    if pt is not None:
        return pt
    return "Error decoding: not valid AES-256-GCM ciphertext"


def format_temperature_fahrenheit_display(value: Any) -> str:
    # UI temp display in Fahrenheit (stored values are Celsius).
    if value is None:
        return "-"
    s = str(value).strip()
    if not s or s.upper() == "N/A":
        return "-"
    sl = s.lower()
    if "error" in sl:
        if any(
            x in sl
            for x in ("decod", "padding", "decrypt", "base64")
        ):
            return "Encrypting…"
        return "-"
    try:
        celsius = float(s)
    except (TypeError, ValueError):
        return s
    fahrenheit = celsius * (9.0 / 5.0) + 32.0
    return f"{fahrenheit:.1f}"


def ingest_field_plaintext(raw: Optional[str]) -> Optional[str]:
    # Normalize incoming fields: decrypt when needed, else keep plaintext.
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    gcm_pt = try_aes256_gcm_decrypt_b64(s)
    if gcm_pt is not None:
        return gcm_pt

    return s


# keep old helper name used in older call sites
def decrypt_data(data: str) -> str:
    if not data or data == "N/A":
        return data
    pt = try_aes256_gcm_decrypt_b64(data)
    if pt is not None:
        return pt
    try:
        return str(data)
    except Exception as e:
        return f"Error decoding: {e}"


def decrypt_stored_reading_field(blob: Optional[str]) -> Optional[str]:
    # decrypt DB field (returns plaintext or None)
    if blob is None:
        return None
    blob_s = str(blob).strip()
    if not blob_s:
        return None
    return try_aes256_gcm_decrypt_b64(blob_s)


def _pkcs7_unpad(data: bytes) -> bytes:
    # Remove PKCS#7 padding for AES-CBC biometric payloads.
    if not data:
        raise ValueError("empty ciphertext")
    pad = data[-1]
    if pad < 1 or pad > 16 or len(data) < pad:
        raise ValueError("invalid PKCS#7 padding")
    if data[-pad:] != bytes([pad]) * pad:
        raise ValueError("invalid PKCS#7 padding")
    return data[:-pad]


def decrypt_biometric_aes128_cbc_b64(
    b64_ciphertext: str,
) -> Tuple[Optional[dict[str, Any]], Optional[str]]:
    # Decrypt biometric.ino payload. Returns (json_dict, error_message).
    raw = (b64_ciphertext or "").strip()
    if not raw:
        return None, "empty body"
    # biometric.ino pads to 16 bytes; match that here.

    def _pad16(s: str) -> bytes:
        b = s.encode("utf-8")
        return (b[:16] + b"\x00" * 16)[:16]

    key_s = _pad16(os.getenv("BIOMETRIC_AES_KEY") or "ThisIsKeyAES333")
    iv_s = _pad16(os.getenv("BIOMETRIC_AES_IV") or "ThisIsVectorIV7")
    try:
        blob = base64.b64decode(raw)
    except Exception as e:
        return None, f"invalid base64: {e}"
    try:
        cipher = AES.new(key_s, AES.MODE_CBC, iv_s)
        plain = _pkcs7_unpad(cipher.decrypt(blob))
        obj = json.loads(plain.decode("utf-8"))
    except Exception as e:
        return None, f"decrypt/json: {e}"
    if not isinstance(obj, dict):
        return None, "inner payload is not a JSON object"
    return obj, None


# map raw gyro/activity to motion stress, then invert to restful score
RESTLESSNESS_MPU_STILL_MAX = 0.10
RESTLESSNESS_MPU_MODERATE_MAX = 0.22
# cap raw MPU variance mapping at this point
RESTLESSNESS_MPU_RAW_CAP = 0.36
RESTLESSNESS_BIOMETRIC_SCALE_MIN = 1.0


def _motion_stress_0_still_100_restless(raw: Any) -> Optional[float]:
    # internal axis: 0 still, 100 restless
    if raw is None:
        return None
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if v > RESTLESSNESS_BIOMETRIC_SCALE_MIN:
        return round(min(100.0, max(0.0, v)), 2)
    if v <= 0.0:
        return 0.0
    a, b, cap = (
        RESTLESSNESS_MPU_STILL_MAX,
        RESTLESSNESS_MPU_MODERATE_MAX,
        RESTLESSNESS_MPU_RAW_CAP,
    )
    if v <= a:
        sc = (v / a) * 20.0 if a > 0 else 0.0
    elif v <= b:
        sc = 20.0 + ((v - a) / (b - a)) * 30.0 if (b - a) > 0 else 35.0
    else:
        span = cap - b
        t = min(1.0, max(0.0, (v - b) / span)) if span > 0 else 1.0
        sc = 50.0 + t * 50.0
    return round(min(100.0, max(0.0, sc)), 2)


def restlessness_score_from_raw(raw: Any) -> Optional[float]:
    # convert stored gyro/activity to restful efficiency (0..100)
    stress = _motion_stress_0_still_100_restless(raw)
    if stress is None:
        return None
    return round(100.0 - stress, 2)


def format_restlessness_band_from_score(score: float) -> Optional[str]:
    # label for restful efficiency band
    if math.isnan(score) or math.isinf(score):
        return None
    sc = min(100.0, max(0.0, score))
    if sc >= 80.0:
        return "Excellent (still / restful)"
    if sc >= 50.0:
        return "Moderate rest efficiency"
    return "Low rest efficiency (restless)"


def format_restlessness_label_from_float(raw_value: float) -> Optional[str]:
    # helper: raw float -> label
    sc = restlessness_score_from_raw(raw_value)
    if sc is None:
        return None
    return format_restlessness_band_from_score(sc)


def format_restlessness_label(raw: Any) -> Optional[str]:
    # helper: stored raw value -> label
    sc = restlessness_score_from_raw(raw)
    if sc is None:
        return None
    return format_restlessness_band_from_score(sc)
