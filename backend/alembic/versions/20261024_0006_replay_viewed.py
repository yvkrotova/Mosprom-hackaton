"""Replay у источника (replay_path, SHA-256 видео), запуск, просмотр события, замеры записи

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("replay_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("video_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("video_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("run_started_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("processing_stats") as batch:
        batch.add_column(sa.Column("frame_ms_avg", sa.Float(), nullable=True))
        batch.add_column(sa.Column("recorded_inference_ms_avg", sa.Float(), nullable=True))
        batch.add_column(sa.Column("recorded_inference_ms_p50", sa.Float(), nullable=True))
        batch.add_column(sa.Column("recorded_inference_ms_p95", sa.Float(), nullable=True))
        batch.add_column(sa.Column("frames_unanalyzed", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("replay_file", sa.String(300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("processing_stats") as batch:
        for name in (
            "replay_file",
            "frames_unanalyzed",
            "recorded_inference_ms_p95",
            "recorded_inference_ms_p50",
            "recorded_inference_ms_avg",
            "frame_ms_avg",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("events") as batch:
        batch.drop_column("viewed_at")
    with op.batch_alter_table("sources") as batch:
        for name in ("run_started_at", "video_fingerprint", "video_sha256", "replay_path"):
            batch.drop_column(name)
