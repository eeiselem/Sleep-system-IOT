from datetime import datetime, timezone


def to_utc_datetime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat_z(value):
    utc_value = to_utc_datetime(value)
    if utc_value is None:
        return None
    return utc_value.isoformat().replace("+00:00", "Z")


def parse_local_datetime_to_utc(datetime_str):
    if not datetime_str:
        return None
    naive_local = datetime.fromisoformat(datetime_str)
    local_tz = datetime.now().astimezone().tzinfo
    local_aware = naive_local.replace(tzinfo=local_tz)
    return local_aware.astimezone(timezone.utc)
