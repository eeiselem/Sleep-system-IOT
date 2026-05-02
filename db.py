"""
Database initialization module.

Separated from app.py to prevent circular dependencies between
the main application, data schemas, and CRUD operations.
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
