"""Журнал событий: эпизоды нарушений, доказательства, действия оператора."""

from typing import Any, Self

from pydantic import Field, model_validator

from app.schemas.common import ApiModel, BBox, InputModel, Page, UtcDatetime
from app.schemas.enums import (
    EpisodeState,
    EvidenceKind,
    OperatorActionType,
    Priority,
    ReviewStatus,
    ScenarioType,
)


class EvidenceBBox(ApiModel):
    label: str
    confidence: float
    bbox: BBox
    track_id: int | None = None


class EvidenceOut(ApiModel):
    id: str
    event_id: str
    kind: EvidenceKind
    url: str = Field(description="Относительный URL: /media/evidence/<event_id>/<file>.jpg")
    video_ts: float | None
    frame_time: UtcDatetime | None
    bboxes: list[EvidenceBBox]
    created_at: UtcDatetime


class OperatorActionOut(ApiModel):
    id: str
    event_id: str
    action: OperatorActionType
    prev_status: ReviewStatus
    new_status: ReviewStatus
    comment: str | None
    author: str
    created_at: UtcDatetime


class EventOut(ApiModel):
    """Событие = один эпизод нарушения. Используется в журнале и в WS-сообщениях."""

    id: str
    scenario_type: ScenarioType
    source_id: str
    source_name: str | None = Field(description="null, если источник удалён")
    zone_id: str | None
    zone_name: str | None
    track_id: int | None
    priority: Priority
    state: EpisodeState
    review_status: ReviewStatus
    confidence: float = Field(description="Максимальная уверенность за эпизод")
    started_at: UtcDatetime
    last_seen_at: UtcDatetime
    ended_at: UtcDatetime | None
    video_ts_start: float
    video_ts_end: float
    video_ts_detected: float = Field(description="Момент подтверждения по таймлайну видео, с")
    detected_at: UtcDatetime = Field(description="Подтверждение эпизода конвейером")
    received_at: UtcDatetime = Field(description="Приём события backend")
    notified_at: UtcDatetime | None = Field(description="Отправка event.created в WebSocket")
    model_version: str
    rule_id: str | None
    rule_version: int | None
    description: str | None
    details: dict[str, Any] | None = Field(
        None,
        description="Данные сценария. run_fast_movement: speed, speed_max — «ростов в секунду»",
    )
    thumbnail_url: str | None = Field(description="URL аннотированного кадра, если есть")
    evidence: list[EvidenceOut]
    created_at: UtcDatetime
    updated_at: UtcDatetime


class EventDetail(EventOut):
    actions: list[OperatorActionOut] = Field(description="История действий, по возрастанию времени")


EventPage = Page[EventOut]


class OperatorActionCreate(InputModel):
    action: OperatorActionType
    comment: str | None = Field(None, max_length=2000)
    author: str = Field(min_length=1, max_length=100, examples=["Иванова А.С."])

    @model_validator(mode="after")
    def _comment_required(self) -> Self:
        if self.action == OperatorActionType.COMMENT and not (self.comment or "").strip():
            raise ValueError("для action=comment нужен непустой comment")
        return self
