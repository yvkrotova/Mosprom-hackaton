"""Смены руководителей: кто отвечал за участок в какое время (для рейтинга безопасности)."""

from datetime import datetime

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, UtcDateTime


class Shift(IdMixin, TimestampMixin, Base):
    __tablename__ = "shifts"
    __table_args__ = (Index("ix_shifts_started_at", "started_at"),)

    supervisor: Mapped[str] = mapped_column(String(100))
    # Участок (Source.site); null — смена отвечает за все камеры.
    site: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)  # null — смена идёт
