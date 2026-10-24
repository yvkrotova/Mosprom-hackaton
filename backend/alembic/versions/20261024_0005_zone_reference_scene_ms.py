"""zones.reference_path (эталон пустого прохода), processing_stats.scene_ms_avg

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("zones") as batch:
        batch.add_column(sa.Column("reference_path", sa.Text(), nullable=True))
    with op.batch_alter_table("processing_stats") as batch:
        batch.add_column(sa.Column("scene_ms_avg", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("processing_stats") as batch:
        batch.drop_column("scene_ms_avg")
    with op.batch_alter_table("zones") as batch:
        batch.drop_column("reference_path")
