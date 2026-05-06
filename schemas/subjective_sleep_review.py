from sqlalchemy import UniqueConstraint

from db import db
from utils import get_current_utc_time

"""Morning subjective review rows (1-5 rating + optional notes)."""


class SubjectiveSleepReview(db.Model):
    __tablename__ = "morning_sleep_feedback"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "feedback_for_date",
            name="uq_morning_feedback_user_date",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    feedback_for_date = db.Column(db.Date, nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    linked_sleep_session_id = db.Column(
        db.Integer,
        db.ForeignKey("sleep_sessions.id"),
        nullable=True,
        index=True,
    )
    algorithm_readiness_snapshot = db.Column(db.Float, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=get_current_utc_time,
    )


# alias kept for old imports
MorningSleepFeedback = SubjectiveSleepReview
