"""Источники видео, зоны и правила."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, UtcDateTime
from app.schemas.enums import Priority, SourceStatus


class Source(IdMixin, TimestampMixin, Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    uri: Mapped[str] = mapped_column(Text)
    site: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default=SourceStatus.IDLE)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    analysis_fps: Mapped[float] = mapped_column(Float)
    # Сценарии, правила по умолчанию для которых уже создавались (ensure_default_rules). Удалённое
    # или выключенное пользователем автоправило по этому списку заново не создаётся.
    defaults_applied: Mapped[list[str] | None] = mapped_column(JSON, default=list)
    # Предзаписанные детекции (replay) этого источника: путь к JSONL на сервере. Пусто — ищется
    # в REPLAY_DIR по SHA-256 видео, затем REPLAY_PATH (см. replay_service).
    replay_path: Mapped[str | None] = mapped_column(Text)
    # SHA-256 видеофайла (кеш) и «отпечаток» файла (размер:mtime), при котором он посчитан.
    video_sha256: Mapped[str | None] = mapped_column(String(64))
    video_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Начало последнего запуска анализа — к нему относятся замеры и задержки в /metrics.
    run_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    zones: Mapped[list["Zone"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )
    rules: Mapped[list["Rule"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class Zone(IdMixin, TimestampMixin, Base):
    __tablename__ = "zones"

    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    polygon: Mapped[list[list[float]]] = mapped_column(JSON)
    # Эталон пустого прохода: кадр источника, сохранённый пользователем (путь от STORAGE_DIR).
    reference_path: Mapped[str | None] = mapped_column(Text)

    source: Mapped[Source] = relationship(back_populates="zones")

    @property
    def reference_url(self) -> str | None:
        return f"/media/{self.reference_path}" if self.reference_path else None


class Rule(IdMixin, TimestampMixin, Base):
    __tablename__ = "rules"

    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    # Удаление зоны удаляет и её правила.
    zone_id: Mapped[str | None] = mapped_column(ForeignKey("zones.id", ondelete="CASCADE"))
    scenario_type: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[str] = mapped_column(String(16), default=Priority.HIGH)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Создано автоматически (сценарий с auto_enable) — фронт помечает «по умолчанию».
    auto_created: Mapped[bool] = mapped_column(Boolean, default=False)

    source: Mapped[Source] = relationship(back_populates="rules")
