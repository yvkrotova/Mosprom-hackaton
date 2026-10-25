"""Смены руководителей (рейтинг безопасности) и вызов экстренных служб по событию

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shifts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("supervisor", sa.String(100), nullable=False),
        sa.Column("site", sa.String(200), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_shifts_started_at", "shifts", ["started_at"])
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("emergency_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("emergency_service", sa.String(100), nullable=True))
        batch.add_column(sa.Column("emergency_by", sa.String(100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("events") as batch:
        for name in ("emergency_by", "emergency_service", "emergency_at"):
            batch.drop_column(name)
    op.drop_index("ix_shifts_started_at", table_name="shifts")
    op.drop_table("shifts")
