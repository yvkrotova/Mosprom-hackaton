"""Журнал: события (эпизоды), доказательства, действия оператора.

У events нет внешних ключей на sources/zones/rules: журнал переживает удаление источника.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, TimestampMixin, UtcDateTime, utcnow
from app.schemas.enums import ReviewStatus


class Event(IdMixin, TimestampMixin, Base):
    __tablename__ = "events"

    scenario_type: Mapped[str] = mapped_column(String(32), index=True)
    source_id: Mapped[str] = mapped_column(String(36), index=True)
    zone_id: Mapped[str | None] = mapped_column(String(36), index=True)
    track_id: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[str] = mapped_column(String(16))
    # state меняет только конвейер, review_status — только оператор.
    state: Mapped[str] = mapped_column(String(16), index=True)
    review_status: Mapped[str] = mapped_column(
        String(16), default=ReviewStatus.UNREVIEWED, index=True
    )
    confidence: Mapped[float] = mapped_column(Float)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    video_ts_start: Mapped[float] = mapped_column(Float)
    video_ts_end: Mapped[float] = mapped_column(Float)
    video_ts_detected: Mapped[float] = mapped_column(Float)
    detected_at: Mapped[datetime] = mapped_column(UtcDateTime)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    alerts_sent: Mapped[int] = mapped_column(Integer, default=0)

    model_version: Mapped[str] = mapped_column(String(200))
    rule_id: Mapped[str | None] = mapped_column(String(36))
    rule_version: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    # Данные сценария: например {"speed": 2.1, "speed_max": 2.6} у run_fast_movement.
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Evidence.created_at",
    )
    actions: Mapped[list["OperatorAction"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OperatorAction.created_at",
    )


class Evidence(IdMixin, Base):
    __tablename__ = "evidence"
    # Повторная загрузка того же кадра не дублирует запись.
    __table_args__ = (UniqueConstraint("event_id", "kind", "video_ts", name="uq_evidence_frame"),)

    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)  # относительно STORAGE_DIR
    url: Mapped[str] = mapped_column(Text)
    video_ts: Mapped[float | None] = mapped_column(Float)
    frame_time: Mapped[datetime | None] = mapped_column(UtcDateTime)
    bboxes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    event: Mapped[Event] = relationship(back_populates="evidence")


class OperatorAction(IdMixin, Base):
    __tablename__ = "operator_actions"

    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(32))
    prev_status: Mapped[str] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    event: Mapped[Event] = relationship(back_populates="actions")
