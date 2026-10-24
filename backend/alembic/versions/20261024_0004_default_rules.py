"""sources.defaults_applied, rules.auto_created: правила по умолчанию

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("defaults_applied", sa.JSON(), nullable=True))
    with op.batch_alter_table("rules") as batch:
        batch.add_column(
            sa.Column("auto_created", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("rules") as batch:
        batch.drop_column("auto_created")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("defaults_applied")
