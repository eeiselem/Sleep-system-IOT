import base64
import os
import hashlib
from typing import Optional
from datetime import datetime, timezone
from dotenv import load_dotenv
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

# look for .env file and load environment variables from it to memor
load_dotenv()

# Same secret string as MASTER_ENCRYPTION_KEY (.env); SHA-256 expands to AES-256 key bytes.
_master_raw = os.getenv("MASTER_ENCRYPTION_KEY") or ""
master_key = _master_raw.encode("utf-8")
aes_256_key = hashlib.sha256(master_key).digest()


def get_current_utc_time():
    return datetime.now(timezone.utc)


def try_aes256_gcm_decrypt_b64(blob_b64: str) -> Optional[str]:
    """
    AES-256-GCM decrypt for transport / at-rest blobs:
    base64(nonce12 || tag16 || ciphertext).
    Returns plaintext UTF-8 string, or None if format/tag is invalid.
    """
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
    if data is None or data == "N/A":
        return data

    plaintext = str(data).encode("utf-8")
    nonce = get_random_bytes(12)
    cipher = AES.new(aes_256_key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    encrypted_blob = nonce + tag + ciphertext
    return base64.b64encode(encrypted_blob).decode("utf-8")


def decrypt_at_rest(data: str) -> str:
    """Strict AES-256-GCM envelope helper (SQLite at-rest payloads)."""
    if not data or data == "N/A":
        return data
    pt = try_aes256_gcm_decrypt_b64(str(data))
    if pt is not None:
        return pt
    return "Error decoding: not valid AES-256-GCM ciphertext"


def ingest_field_plaintext(raw: Optional[str]) -> Optional[str]:
    """
    Normalize /post-data field values after JSON parse:
    • AES-256-GCM transport blobs (nonce||tag||ciphertext, Base64 — matches firmware)
    • Plain numeric/string payloads (development / plaintext edge hardware only)
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    gcm_pt = try_aes256_gcm_decrypt_b64(s)
    if gcm_pt is not None:
        return gcm_pt

    return s


# Backwards compatibility for call sites that referenced decrypt_data
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
    """Decrypt DB columns storing AES-256-GCM ciphertext (Base64)."""
    if blob is None:
        return None
    blob_s = str(blob).strip()
    if not blob_s:
        return None
    return try_aes256_gcm_decrypt_b64(blob_s)
