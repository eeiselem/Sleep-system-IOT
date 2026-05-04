"""Flask config and environment-backed constants (single place for tuning knobs)."""
import os
from pathlib import Path

from crud.reading import EVENT_LOG_METRIC_LABELS


class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = "sqlite:///server.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False


ROOT = Path(__file__).resolve().parent

# proposal defaults based on medical data sources
SCI_TEMP_BAND_C_MIN = 15.0
SCI_TEMP_BAND_C_MAX = 19.0
SCI_HUMIDITY_PCT_MIN = 40.0
SCI_HUMIDITY_PCT_MAX = 60.0
SCI_LUX_MIN = 0.0
SCI_LUX_MAX = 10.0

try:
    MQ135_SAFE_INDEX_MAX = float(os.getenv("MQ135_SAFE_INDEX_MAX", "420"))
except ValueError:
    MQ135_SAFE_INDEX_MAX = 420.0

NOISE_SPIKE_DELTA_DB = 7.0
NOISE_SPIKE_ABOVE_BASELINE_DB = 12.0
NOISE_SUSTAINED_ABOVE_BASELINE_DB = 5.0
NOISE_SUSTAINED_STREAK_TICKS = 3
NOISE_HIGH_ABSOLUTE_DB = 48.0

EVENT_LOG_BREACH_METRICS = frozenset(
    {
        "heart_rate",
        "spo2",
        "ambient_noise",
        "air_quality",
        "gyro_variance",
    }
)
EVENT_LOG_BREACH_LABELS = {
    k: EVENT_LOG_METRIC_LABELS[k] for k in EVENT_LOG_BREACH_METRICS
}

SUBJECTIVE_DISCREPANCY_MAX_STARS = 1
SUBJECTIVE_DISCREPANCY_MIN_ALGORITHM_READINESS = 70.0
