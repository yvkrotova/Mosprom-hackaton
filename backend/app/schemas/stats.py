"""Статистика журнала, метрики производительности и здоровье системы."""

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel, UtcDatetime
from app.schemas.enums import SourceStatus


class CountItem(BaseModel):
    key: str = Field(description="scenario_type, id источника или id зоны")
    label: str | None = Field(None, description="Имя источника/зоны для отображения")
    count: int


class HourCount(BaseModel):
    hour: UtcDatetime = Field(description="Начало часа, UTC")
    count: int


class ReviewCounts(BaseModel):
    unreviewed: int
    confirmed: int
    false_positive: int
    escalated: int


class StatsOut(BaseModel):
    from_: UtcDatetime | None = Field(serialization_alias="from")
    to: UtcDatetime | None
    total: int
    by_scenario_type: list[CountItem]
    by_source: list[CountItem]
    by_zone: list[CountItem]
    by_hour: list[HourCount]
    review: ReviewCounts


class SourceMetrics(BaseModel):
    """Последний замер воркера по источнику (из processing_stats)."""

    source_id: str
    source_name: str | None
    ts: UtcDatetime | None
    fps: float | None = Field(description="Фактическая частота обработки, кадр/с")
    inference_ms_avg: float | None
    inference_ms_p95: float | None
    preprocess_ms_avg: float | None = Field(None, description="Подготовка входа сети, мс")
    postprocess_ms_avg: float | None = Field(None, description="Декодирование выхода и NMS, мс")
    frames_processed: int | None
    detector_kind: str | None = Field(
        description="mock | replay | onnx_cpu (локальный CPU, не LinQ) | linq"
    )
    model_version: str | None


class LatencyMetrics(BaseModel):
    events_count: int
    detection_delay_real_s_avg: float | None = Field(
        description="Среднее (detected_at − started_at): реальное время отклика, с"
    )
    detection_delay_real_s_p95: float | None
    detection_delay_video_ts_s_avg: float | None = Field(
        description="Среднее (video_ts_detected − video_ts_start): задержка по таймлайну видео, с"
    )
    detection_delay_video_ts_s_p95: float | None
    notify_delay_ms_avg: float | None = Field(description="Среднее (notified_at − detected_at), мс")
    notify_delay_ms_p95: float | None
    alerts_per_episode: float | None = Field(
        description="Среднее число оповещений event.created на эпизод (должно быть 1.0)"
    )


class MetricsOut(BaseModel):
    sources: list[SourceMetrics]
    latency: LatencyMetrics


class SourceHealth(ApiModel):
    id: str
    name: str
    status: SourceStatus
    last_heartbeat_at: UtcDatetime | None
    last_error: str | None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"] = Field(
        description="degraded, если БД недоступна или есть источник в error/disconnected"
    )
    db: Literal["ok", "error"]
    detector_kind: str
    sources: list[SourceHealth]
