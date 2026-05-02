from datetime import date
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


# pydantic models catch data before hits the database, validate and cleans it


class AppBaseModel(BaseModel):
    # form_attributes allows Pydantic to read from ORM objects directly
    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    @field_validator("*", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Any:
        """
        Intercepts all incoming data and converts blank or whitespace-only
        strings into None (Null) to prevent database corruption.
        """
        # only attempt to strip() if the incoming data is text
        if isinstance(v, str) and not v.strip():
            return None
        # If valid text or number, let it pass through unchanged
        return v


# ensure data is two strings after AppBaseModel does cleaning and validation
class ReadingBase(AppBaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        from_attributes=True,
        populate_by_name=True,
    )

    temperature: str
    humidity: str
    air_quality: str = Field(validation_alias=AliasChoices("air_quality", "voc_level"))
    ambient_noise: str
    ambient_light: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ambient_light", "light_level"),
    )
    heart_rate: str
    spo2: str
    gyro_variance: str


class SubjectiveSleepReviewIn(AppBaseModel):
    """
    Request body for POST /api/subjective-sleep-review.

    Persisted by SQLAlchemy model ``schemas.subjective_sleep_review.SubjectiveSleepReview``
    (physical table ``morning_sleep_feedback``).
    """

    rating: int = Field(ge=1, le=5)
    notes: Optional[str] = None
    session_date: Optional[date] = None
