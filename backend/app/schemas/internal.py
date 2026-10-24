"""Внутренний API между воркером конвейера и backend (заголовок X-Internal-Token)."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import InputModel, UtcDatetime, UuidStr
from app.schemas.enums import EpisodeState, EvidenceKind, Priority, ScenarioType, UpsertOutcome
from app.schemas.events import EvidenceBBox
from app.schemas.rules import RuleOut
from app.schemas.sources import SourceOut, ZoneOut


class EventUpsert(InputModel):
    """Полное состояние эпизода. Повторная отправка того же тела безопасна (идемпотентно)."""

    source_id: UuidStr
    scenario_type: ScenarioType
    zone_id: UuidStr | None
    track_id: int | None
    priority: Priority
    state: EpisodeState
    confidence: float = Field(ge=0, le=1)
    started_at: UtcDatetime
    last_seen_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    video_ts_start: float = Field(ge=0)
    video_ts_end: float = Field(ge=0)
    video_ts_detected: float = Field(
        ge=0, description="video_ts кадра, на котором эпизод подтверждён"
    )
    detected_at: UtcDatetime
    model_version: str
    rule_id: UuidStr | None
    rule_version: int | None
    description: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.video_ts_end < self.video_ts_start:
            raise ValueError("video_ts_end < video_ts_start")
        if self.video_ts_detected < self.video_ts_start:
            raise ValueError("video_ts_detected < video_ts_start")
        if self.state == EpisodeState.ENDED and self.ended_at is None:
            raise ValueError("для state=ended нужен ended_at")
        return self


class EventUpsertResult(BaseModel):
    event_id: str
    outcome: UpsertOutcome


class EvidenceMeta(InputModel):
    """JSON-часть multipart-запроса POST /internal/events/{id}/evidence (поле metadata)."""

    kind: EvidenceKind
    video_ts: float | None = Field(None, ge=0)
    frame_time: UtcDatetime | None = None
    bboxes: list[EvidenceBBox] = Field(default_factory=list)


class HeartbeatIn(InputModel):
    source_id: UuidStr
    status: Literal["running", "stopped", "error"]
    fps: float | None = Field(None, ge=0)
    inference_ms_avg: float | None = Field(None, ge=0)
    inference_ms_p95: float | None = Field(None, ge=0)
    preprocess_ms_avg: float | None = Field(None, ge=0)
    postprocess_ms_avg: float | None = Field(None, ge=0)
    frames_processed: int = Field(0, ge=0)
    model_version: str | None = None
    detector_kind: str | None = None
    error: str | None = Field(None, max_length=4000)


class PipelineSettings(BaseModel):
    episode_update_interval_s: float
    heartbeat_interval_s: float


class SourceConfig(BaseModel):
    """Всё, что нужно воркеру для запуска: источник, его зоны и включённые правила."""

    source: SourceOut
    zones: list[ZoneOut]
    rules: list[RuleOut]
    settings: PipelineSettings
