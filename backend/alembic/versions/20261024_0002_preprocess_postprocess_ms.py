"""processing_stats: preprocess_ms_avg, postprocess_ms_avg

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("processing_stats") as batch:
        batch.add_column(sa.Column("preprocess_ms_avg", sa.Float(), nullable=True))
        batch.add_column(sa.Column("postprocess_ms_avg", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("processing_stats") as batch:
        batch.drop_column("postprocess_ms_avg")
        batch.drop_column("preprocess_ms_avg")
