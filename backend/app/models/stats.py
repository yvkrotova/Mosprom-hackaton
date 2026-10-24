"""Замеры производительности воркера (по одному на heartbeat)."""

from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import IdMixin, UtcDateTime, utcnow


class ProcessingStat(IdMixin, Base):
    __tablename__ = "processing_stats"
    __table_args__ = (Index("ix_processing_stats_source_ts", "source_id", "ts"),)

    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    fps: Mapped[float | None] = mapped_column(Float)
    inference_ms_avg: Mapped[float | None] = mapped_column(Float)
    inference_ms_p95: Mapped[float | None] = mapped_column(Float)
    preprocess_ms_avg: Mapped[float | None] = mapped_column(Float)
    postprocess_ms_avg: Mapped[float | None] = mapped_column(Float)
    scene_ms_avg: Mapped[float | None] = mapped_column(Float)
    # Полное время обработки кадра этим процессом (для replay — скорость воспроизведения).
    frame_ms_avg: Mapped[float | None] = mapped_column(Float)
    # Replay: историческое время инференса из записи (на LinQ), не замер этого процесса.
    recorded_inference_ms_avg: Mapped[float | None] = mapped_column(Float)
    recorded_inference_ms_p50: Mapped[float | None] = mapped_column(Float)
    recorded_inference_ms_p95: Mapped[float | None] = mapped_column(Float)
    frames_unanalyzed: Mapped[int | None] = mapped_column(Integer)
    replay_file: Mapped[str | None] = mapped_column(String(300))
    frames_processed: Mapped[int] = mapped_column(Integer, default=0)
    model_version: Mapped[str | None] = mapped_column(String(200))
    detector_kind: Mapped[str | None] = mapped_column(String(32))
