from db import db
from flask_login import UserMixin

"""User account model plus per-user comfort/config settings."""


# defines database model for the users table
# stores user credentials and roles
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(20), default="User")
    cfg_temp_min = db.Column(db.Float, nullable=True)
    cfg_temp_max = db.Column(db.Float, nullable=True)
    cfg_noise_limit = db.Column(db.Float, nullable=True)
    cfg_wake_time = db.Column(db.String(5), nullable=True)
    cfg_guardrail_temp_f_min = db.Column(db.Float, nullable=True)
    cfg_guardrail_temp_f_max = db.Column(db.Float, nullable=True)
    cfg_optimal_band_f_min = db.Column(db.Float, nullable=True)
    cfg_optimal_band_f_max = db.Column(db.Float, nullable=True)
    cfg_override_optimal_band = db.Column(db.Boolean, default=False)
