"""baseline schema patch

Revision ID: 20260505_01
Revises:
Create Date: 2026-05-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260505_01"
down_revision = None
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table(table_name):
        return set()
    return {c["name"] for c in insp.get_columns(table_name)}


def _add_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name in _column_names(table_name):
        return
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(column)


def upgrade() -> None:
    _add_if_missing(
        "readings",
        sa.Column("air_quality", sa.String(length=255), nullable=True),
    )
    _add_if_missing(
        "readings",
        sa.Column("ambient_noise", sa.String(length=255), nullable=True),
    )
    _add_if_missing(
        "readings",
        sa.Column("ambient_light", sa.String(length=255), nullable=True),
    )
    _add_if_missing(
        "readings",
        sa.Column("heart_rate", sa.String(length=255), nullable=True),
    )
    _add_if_missing(
        "readings",
        sa.Column("spo2", sa.String(length=255), nullable=True),
    )
    _add_if_missing(
        "readings",
        sa.Column("gyro_variance", sa.String(length=255), nullable=True),
    )
    _add_if_missing(
        "readings",
        sa.Column("hrv_rmssd", sa.String(length=255), nullable=True),
    )
    _add_if_missing(
        "readings",
        sa.Column("user_id", sa.Integer(), nullable=True),
    )

    cols = _column_names("readings")
    if "temperature" in cols or "humidity" in cols:
        with op.batch_alter_table("readings") as batch_op:
            if "temperature" in cols:
                batch_op.alter_column(
                    "temperature",
                    existing_type=sa.String(),
                    nullable=True,
                )
            if "humidity" in cols:
                batch_op.alter_column(
                    "humidity",
                    existing_type=sa.String(),
                    nullable=True,
                )

    _add_if_missing(
        "sleep_sessions",
        sa.Column("user_id", sa.Integer(), nullable=True),
    )

    _add_if_missing(
        "users",
        sa.Column("cfg_temp_min", sa.Float(), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_temp_max", sa.Float(), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_noise_limit", sa.Float(), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_wake_time", sa.String(length=5), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_guardrail_temp_f_min", sa.Float(), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_guardrail_temp_f_max", sa.Float(), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_optimal_band_f_min", sa.Float(), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_optimal_band_f_max", sa.Float(), nullable=True),
    )
    _add_if_missing(
        "users",
        sa.Column("cfg_override_optimal_band", sa.Boolean(), nullable=True),
    )

    _add_if_missing(
        "morning_sleep_feedback",
        sa.Column("linked_sleep_session_id", sa.Integer(), nullable=True),
    )
    _add_if_missing(
        "morning_sleep_feedback",
        sa.Column("algorithm_readiness_snapshot", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    # Baseline migration; keep downgrade as no-op for safety.
    pass
