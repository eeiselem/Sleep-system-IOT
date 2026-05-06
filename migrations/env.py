from __future__ import with_statement

from alembic import context
from sqlalchemy import engine_from_config, pool

from db import db
from schemas import (
    micro_arousal_event,
    reading,
    sleep_score_discrepancy_log,
    sleep_session,
    subjective_sleep_review,
    user,
)

config = context.config
# Model module imports are intentionally referenced
# so SQLAlchemy metadata is fully populated.
_MODEL_MODULES = (
    micro_arousal_event,
    reading,
    sleep_score_discrepancy_log,
    sleep_session,
    subjective_sleep_review,
    user,
)
target_metadata = db.metadata


def _db_url() -> str:
    xargs = context.get_x_argument(as_dictionary=True)
    return xargs.get("db_url") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
