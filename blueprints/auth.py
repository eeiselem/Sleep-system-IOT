from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_user, logout_user

from db import db
from extensions import bcrypt, login_manager
from schemas.user import User

bp = Blueprint("auth", __name__)


@login_manager.user_loader
def load_user(user_id):
    # Flask-Login callback: restore user from session id.
    return db.session.get(User, int(user_id))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # Plain form auth for project demo accounts.
        username = request.form.get("username")
        password = request.form.get("password")
        user = User.query.filter_by(username=username).first()
        if user and bcrypt.check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("dashboard.overview"))
        print("Invalid login attempt")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    # Clear login session and return to login screen.
    logout_user()
    print("User logged out successfully.")
    return redirect(url_for("auth.login"))
