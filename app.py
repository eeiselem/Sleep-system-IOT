import os

from dotenv import load_dotenv
from flask import Flask

from blueprints import register_blueprints
from db import db
from db_schema import ensure_demo_users, run_schema_patches
from extensions import init_extensions, login_manager
from room_sim import init_room_sim, start_background_tasks
from utils import format_temperature_fahrenheit_display

load_dotenv()

app = Flask(__name__)
app.add_template_filter(format_temperature_fahrenheit_display, "temp_f")
app.secret_key = os.getenv("FLASK_SECRET_KEY")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///server.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

init_extensions(app)
login_manager.login_view = "auth.login"
init_room_sim(app)

with app.app_context():
    db.create_all()
    run_schema_patches()
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
