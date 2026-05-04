from datetime import date
from typing import Any, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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


class EnvironmentReadingIn(AppBaseModel):
    """
    Environmental-only board: ``POST /post-environment`` (JSON body, same ingest key as /post-data).

    No MAX301 / vitals — heart_rate and spo2 are omitted (stored NULL). Optional MPU ``gyro_variance``.
    """

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
    gyro_variance: Optional[str] = None


class BiometricPayload(AppBaseModel):
    """
    Inner JSON after AES-128-CBC decrypt from ``biometric.ino`` (POST /biometric).

    ``gyro`` is that sketch's activity rating (%); stored in ``gyro_variance`` column.
    """

    model_config = ConfigDict(
        str_strip_whitespace=True,
        from_attributes=True,
        populate_by_name=True,
    )

    heart_rate: str
    spo2: str
    gyro: str = Field(validation_alias=AliasChoices("gyro", "gyro_variance"))
    hrv: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def stringify_numeric_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for k in ("heart_rate", "spo2", "gyro", "gyro_variance", "hrv"):
            if k in out and out[k] is not None and not isinstance(out[k], str):
                out[k] = str(out[k])
        return out


class SubjectiveSleepReviewIn(AppBaseModel):
    """
    Request body for POST /api/subjective-sleep-review.

    Persisted by SQLAlchemy model ``schemas.subjective_sleep_review.SubjectiveSleepReview``
    (physical table ``morning_sleep_feedback``). The server links each save to the
    matching closed ``SleepSession`` and snapshots algorithmic readiness as ground-truth context.
    """

    rating: int = Field(ge=1, le=5)
    notes: Optional[str] = None
    session_date: Optional[date] = None
