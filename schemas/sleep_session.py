from db import db


class SleepSession(db.Model):
    """
    One closed or in-progress sleep interval: ASLEEP onset → AWAKE (gyro breakout).

    Readiness metrics are filled when ``ended_at`` is set (session complete).
    """

    __tablename__ = "sleep_sessions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    started_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    readiness_score = db.Column(db.Float, nullable=True)
    sleep_efficiency_pct = db.Column(db.Float, nullable=True)
    sleep_efficiency_score = db.Column(db.Float, nullable=True)
    avg_prv_ms = db.Column(db.Float, nullable=True)
    prv_score = db.Column(db.Float, nullable=True)
    spo2_drop_count = db.Column(db.Integer, nullable=True)
    noise_spike_count = db.Column(db.Integer, nullable=True)
    disturbance_score = db.Column(db.Float, nullable=True)
    sample_count = db.Column(db.Integer, nullable=True)
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=True)
