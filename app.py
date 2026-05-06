import os

"""Flask app entrypoint and startup wiring.

This module wires extensions, creates tables for local runs,
registers blueprints, and starts background workers.
"""

from dotenv import load_dotenv
from flask import Flask
from flask_bcrypt import Bcrypt

from blueprints import register_blueprints
from db import db
from extensions import init_extensions
from room_sim import init_room_sim, start_background_tasks
from schemas.user import User
from utils import format_temperature_fahrenheit_display

load_dotenv()

app = Flask(__name__)
app.add_template_filter(format_temperature_fahrenheit_display, "temp_f")
app.secret_key = os.getenv("FLASK_SECRET_KEY")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///server.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

init_extensions(app)
init_room_sim(app)


def ensure_demo_users() -> None:
    bcrypt = Bcrypt(app)
    if not User.query.filter_by(username="admin").first():
        hashed_pw = bcrypt.generate_password_hash("admin").decode("utf-8")
        db.session.add(
            User(username="admin", password_hash=hashed_pw, role="Admin")
        )
        db.session.commit()
        print("--- Admin Account Created: Use 'admin' and 'admin' ---")
    if not User.query.filter_by(username="testuser").first():
        hashed_pw = bcrypt.generate_password_hash(
            "password123"
        ).decode("utf-8")
        db.session.add(
            User(username="testuser", password_hash=hashed_pw, role="User")
        )
        db.session.commit()


with app.app_context():
    db.create_all()
    ensure_demo_users()

register_blueprints(app)
start_background_tasks()

if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", "8888"))
    use_ssl = os.getenv("FLASK_USE_SSL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "",
    )
    if use_ssl:
        app.run(host="0.0.0.0", port=port, ssl_context="adhoc")
    else:
        app.run(host="0.0.0.0", port=port)
