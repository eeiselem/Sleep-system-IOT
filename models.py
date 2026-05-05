from datetime import date
from typing import Any, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class AppBaseModel(BaseModel):
    # allow ORM object parsing
    model_config = ConfigDict(str_strip_whitespace=True, from_attributes=True)

    @field_validator("*", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Any:
        # turn blank strings into None
        if isinstance(v, str) and not v.strip():
            return None
        return v


class ReadingBase(AppBaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        from_attributes=True,
        populate_by_name=True,
    )

    temperature: str
    humidity: str
    air_quality: str = Field(
        validation_alias=AliasChoices("air_quality", "voc_level"),
    )
    ambient_noise: str
    ambient_light: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ambient_light", "light_level"),
    )
    heart_rate: str
    spo2: str
    gyro_variance: str


class EnvironmentReadingIn(AppBaseModel):
    # Payload from env-only board (/post-environment).

    model_config = ConfigDict(
        str_strip_whitespace=True,
        from_attributes=True,
        populate_by_name=True,
    )

    temperature: str
    humidity: str
    air_quality: str = Field(
        validation_alias=AliasChoices("air_quality", "voc_level"),
    )
    ambient_noise: str
    ambient_light: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ambient_light", "light_level"),
    )
    gyro_variance: Optional[str] = None


class BiometricPayload(AppBaseModel):
    # Inner payload after biometric decrypt.

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
    # Body for POST /api/subjective-sleep-review.

    rating: int = Field(ge=1, le=5)
    notes: Optional[str] = None
    session_date: Optional[date] = None
