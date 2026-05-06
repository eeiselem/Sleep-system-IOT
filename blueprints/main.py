from flask import Blueprint, redirect, url_for

bp = Blueprint("main", __name__)


@bp.route("/")
def home():
    # Landing route sends user to login first.
    return redirect(url_for("auth.login"))
