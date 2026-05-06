from __future__ import annotations

"""Reusable read/query helpers for reading rows.

These functions keep filtering and payload shaping logic in one place.
"""

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
    # Generic "latest rows" query used by dashboard and LLM context routes.
    q = Reading.query.filter(Reading.user_id == user_id)
    if since is not None:
        q = q.filter(Reading.timestamp >= since)
    if ascending:
        q = q.order_by(Reading.timestamp.asc(), Reading.id.asc())
    else:
        q = q.order_by(Reading.timestamp.desc(), Reading.id.desc())
    return q.limit(limit).all()


def downsample_rows(rows: List[Reading], target_count: int) -> List[Reading]:
    # Keep temporal spread while reducing payload size.
    if target_count <= 0 or len(rows) <= target_count:
        return rows
    stride = max(1, len(rows) // target_count)
    return rows[::stride][:target_count]


def compact_payload_point(
    point: Dict[str, Any],
    *,
    float_precision: int = 2,
) -> Dict[str, Any]:
    # Drop null keys and round floats so JSON payloads stay small and readable.
    compact: Dict[str, Any] = {}
    for k, v in point.items():
        if v is None:
            continue
        if isinstance(v, float):
            compact[k] = round(v, float_precision)
        else:
            compact[k] = v
    return compact
