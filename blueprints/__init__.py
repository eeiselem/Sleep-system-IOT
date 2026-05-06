def register_blueprints(app):
    # Import inside function to avoid circular imports on startup.
    from blueprints import api as api_bp
    from blueprints import auth as auth_bp
    from blueprints import dashboard as dashboard_bp
    from blueprints import ingest as ingest_bp
    from blueprints import main as main_bp

    modules = (
        auth_bp,
        main_bp,
        dashboard_bp,
        ingest_bp,
        api_bp,
    )
    for module in modules:
        # Each module exposes bp = Blueprint(...).
        app.register_blueprint(module.bp)
