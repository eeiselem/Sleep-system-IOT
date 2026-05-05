from db import db
from utils import get_current_utc_time


class SleepScoreDiscrepancyLog(db.Model):
    __tablename__ = "sleep_score_discrepancy_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    feedback_for_date = db.Column(db.Date, nullable=False, index=True)
    sleep_session_id = db.Column(
        db.Integer, db.ForeignKey("sleep_sessions.id"), nullable=True, index=True
    )
    stars = db.Column(db.Integer, nullable=False)
    algorithm_readiness = db.Column(db.Float, nullable=False)
    formula_version = db.Column(db.Text, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=get_current_utc_time,
    )
