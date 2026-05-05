from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from schemas.reading import Reading


def fetch_recent_user_rows(
    user_id: int,
    *,
    since: Optional[datetime] = None,
    limit: int = 80,
    ascending: bool = False,
) -> List[Reading]:
    q = Reading.query.filter(Reading.user_id == user_id)
    if since is not None:
        q = q.filter(Reading.timestamp >= since)
    if ascending:
        q = q.order_by(Reading.timestamp.asc(), Reading.id.asc())
    else:
        q = q.order_by(Reading.timestamp.desc(), Reading.id.desc())
    return q.limit(limit).all()


def downsample_rows(rows: List[Reading], target_count: int) -> List[Reading]:
    if target_count <= 0 or len(rows) <= target_count:
        return rows
    stride = max(1, len(rows) // target_count)
    return rows[::stride][:target_count]


def compact_payload_point(
    point: Dict[str, Any],
    *,
    float_precision: int = 2,
) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for k, v in point.items():
        if v is None:
            continue
        if isinstance(v, float):
            compact[k] = round(v, float_precision)
        else:
            compact[k] = v
    return compact
