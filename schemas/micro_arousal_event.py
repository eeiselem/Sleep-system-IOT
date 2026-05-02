"""Persisted heuristic events: discrete PRV-drop soon after acoustic spikes."""

from db import db
from utils import get_current_utc_time


class MicroArousalEvent(db.Model):
    __tablename__ = "micro_arousal_events"

    id = db.Column(db.Integer, primary_key=True, index=True)
    detected_at = db.Column(
        db.DateTime(timezone=True),
        default=get_current_utc_time,
        nullable=False,
        index=True,
    )
    spike_noise_db = db.Column(db.Float, nullable=False)
    prv_median_before_ms = db.Column(db.Float, nullable=True)
    prv_observed_ms = db.Column(db.Float, nullable=True)
    prv_drop_ms = db.Column(db.Float, nullable=True)
    disruption_label = db.Column(db.String(160), nullable=False)
