from db import db
from utils import (
    get_current_utc_time,
    decrypt_stored_reading_field,
    encrypt_at_rest,
)


# defines the reading model for the database
# Temperature / humidity match other channels: AES-256-GCM envelopes (see utils / firmware).
class Reading(db.Model):
    __tablename__ = "readings"

    # Primary Key: Unique ID for each database row
    id = db.Column(db.Integer, primary_key=True, index=True)

    # timestamp of when the reading when a row is created
    timestamp = db.Column(
        db.DateTime(timezone=True),
        default=get_current_utc_time,
    )

    # Private Database Columns: AES-256-GCM ciphertext (nonce||tag||ct, Base64).
    _temperature = db.Column("temperature", db.String(512), nullable=False)
    _humidity = db.Column("humidity", db.String(512), nullable=False)
    _air_quality = db.Column("air_quality", db.String(255), nullable=True)
    _ambient_noise = db.Column("ambient_noise", db.String(255), nullable=True)
    _heart_rate = db.Column("heart_rate", db.String(255), nullable=True)
    _spo2 = db.Column("spo2", db.String(255), nullable=True)
    _gyro_variance = db.Column("gyro_variance", db.String(255), nullable=True)
    _ambient_light = db.Column("ambient_light", db.String(255), nullable=True)

    @property
    def temperature(self):
        if self._temperature is None:
            return None
        return decrypt_stored_reading_field(self._temperature)

    @property
    def humidity(self):
        if self._humidity is None:
            return None
        return decrypt_stored_reading_field(self._humidity)

    @property
    def air_quality(self):
        if self._air_quality is None:
            return None
        return decrypt_stored_reading_field(self._air_quality)

    @property
    def ambient_noise(self):
        if self._ambient_noise is None:
            return None
        return decrypt_stored_reading_field(self._ambient_noise)

    @property
    def heart_rate(self):
        if self._heart_rate is None:
            return None
        return decrypt_stored_reading_field(self._heart_rate)

    @property
    def spo2(self):
        if self._spo2 is None:
            return None
        return decrypt_stored_reading_field(self._spo2)

    @property
    def gyro_variance(self):
        if self._gyro_variance is None:
            return None
        return decrypt_stored_reading_field(self._gyro_variance)

    @property
    def ambient_light(self):
        if self._ambient_light is None:
            return None
        return decrypt_stored_reading_field(self._ambient_light)

    @temperature.setter
    def temperature(self, value):
        if value is None:
            self._temperature = None
        else:
            self._temperature = encrypt_at_rest(str(value))

    @humidity.setter
    def humidity(self, value):
        if value is None:
            self._humidity = None
        else:
            self._humidity = encrypt_at_rest(str(value))

    @air_quality.setter
    def air_quality(self, value):
        self._air_quality = None if value is None else encrypt_at_rest(str(value))

    @ambient_noise.setter
    def ambient_noise(self, value):
        self._ambient_noise = None if value is None else encrypt_at_rest(str(value))

    @heart_rate.setter
    def heart_rate(self, value):
        self._heart_rate = None if value is None else encrypt_at_rest(str(value))

    @spo2.setter
    def spo2(self, value):
        self._spo2 = None if value is None else encrypt_at_rest(str(value))

    @gyro_variance.setter
    def gyro_variance(self, value):
        self._gyro_variance = None if value is None else encrypt_at_rest(str(value))

    @ambient_light.setter
    def ambient_light(self, value):
        self._ambient_light = None if value is None else encrypt_at_rest(str(value))
