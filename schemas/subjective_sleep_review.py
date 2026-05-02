"""ORM model for subjective sleep reviews (table ``morning_sleep_feedback``)."""

from sqlalchemy import UniqueConstraint

from db import db
from utils import get_current_utc_time


class SubjectiveSleepReview(db.Model):
    """
    Daily 1–5 star rating and optional text for a specific sleep session.

    ``feedback_for_date`` is the wake morning (UTC calendar day) that ends the
    reviewed night (aligned with sleep readiness / optimal-band keys).
    """

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
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=get_current_utc_time,
    )


# Backward-compatible alias (same table / mapper).
MorningSleepFeedback = SubjectiveSleepReview
