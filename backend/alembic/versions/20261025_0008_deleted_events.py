"""Надгробия удалённых событий; пометка демо-данных у камер и смен

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-25 04:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deleted_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in ("sources", "shifts"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false())
            )


def downgrade() -> None:
    for table in ("shifts", "sources"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("is_demo")
    op.drop_table("deleted_events")
