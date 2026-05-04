"""Flask blueprints."""


def register_blueprints(app):
    from blueprints import api as api_bp
    from blueprints import auth as auth_bp
    from blueprints import dashboard as dashboard_bp
    from blueprints import ingest as ingest_bp
    from blueprints import main as main_bp

    app.register_blueprint(auth_bp.bp)
    app.register_blueprint(main_bp.bp)
    app.register_blueprint(dashboard_bp.bp)
    app.register_blueprint(ingest_bp.bp)
    app.register_blueprint(api_bp.bp)
