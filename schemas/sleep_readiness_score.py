from db import db


class SleepReadinessScore(db.Model):
    __tablename__ = "sleep_readiness_scores"

    id = db.Column(db.Integer, primary_key=True, index=True)
    score_date = db.Column(db.Date, unique=True, nullable=False, index=True)
    readiness_score = db.Column(db.Float, nullable=False)
    sleep_efficiency_pct = db.Column(db.Float, nullable=False)
    sleep_efficiency_score = db.Column(db.Float, nullable=False)
    avg_prv_ms = db.Column(db.Float, nullable=False)
    prv_score = db.Column(db.Float, nullable=False)
    spo2_drop_count = db.Column(db.Integer, nullable=False)
    noise_spike_count = db.Column(db.Integer, nullable=False)
    disturbance_score = db.Column(db.Float, nullable=False)
    sample_count = db.Column(db.Integer, nullable=False)
    calculated_at = db.Column(db.DateTime(timezone=True), nullable=False)
