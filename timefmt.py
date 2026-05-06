from datetime import datetime, timezone

"""Small datetime helpers used across API and scoring code."""


def to_utc_datetime(value):
    # Normalize datetime to UTC.
    # For naive values, keep wall-clock fields and attach UTC tzinfo.
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat_z(value):
    # Render UTC datetime in API-friendly ISO format ending with Z.
    utc_value = to_utc_datetime(value)
    if utc_value is None:
        return None
    return utc_value.isoformat().replace("+00:00", "Z")


def parse_local_datetime_to_utc(datetime_str):
    # Parse browser-local ISO datetime and convert to UTC for DB queries.
    if not datetime_str:
        return None
    naive_local = datetime.fromisoformat(datetime_str)
    local_tz = datetime.now().astimezone().tzinfo
    local_aware = naive_local.replace(tzinfo=local_tz)
    return local_aware.astimezone(timezone.utc)
