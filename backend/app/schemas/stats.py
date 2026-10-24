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


class StatsSummary(BaseModel):
    """Шесть показателей экрана «Статистика». Период — по началу эпизода.
    Инвариант: unreviewed + confirmed + false_positive = total (escalated входит в confirmed)."""

    total: int = Field(description="Эпизодов за период")
    active: int = Field(description="Из них ещё идут")
    unreviewed: int = Field(description="Ждут реакции оператора (не проверено)")
    confirmed: int = Field(description="Подтверждено, включая переданные мастеру")
    escalated: int = Field(description="Из подтверждённых — передано мастеру")
    false_positive: int = Field(description="Ложных срабатываний по результатам проверки")
    avg_zone_time_s: float | None = Field(
        description="Среднее время в зоне: завершённые эпизоды danger_zone, по времени видео; "
        "null — таких эпизодов нет"
    )
    alerts_per_episode: float | None = Field(
        description="Оповещений event.created на эпизод (повторная доставка не считается); "
        "null — эпизодов нет"
    )


class DemoFlag(BaseModel):
    is_demo: bool
    reason: str | None = Field(None, description="Почему данные демонстрационные")


class StatsOut(BaseModel):
    from_: UtcDatetime | None = Field(serialization_alias="from")
    to: UtcDatetime | None
    summary: StatsSummary
    demo: DemoFlag
    total: int
    by_scenario_type: list[CountItem]
    by_source: list[CountItem]
    by_zone: list[CountItem]
    by_hour: list[HourCount]
    review: ReviewCounts


class SourceLatency(BaseModel):
    """Задержки по событиям текущего (последнего) запуска источника."""

    events_count: int
    detection_delay_video_ts_s_avg: float | None = Field(
        description="Среднее (video_ts_detected − video_ts_start): по времени видео, с"
    )
    detection_delay_video_ts_s_p95: float | None
    detection_delay_real_s_avg: float | None = Field(
        description="Среднее (detected_at − started_at): по реальному времени, с"
    )
    detection_delay_real_s_p95: float | None
    notify_delay_ms_avg: float | None = Field(
        description="Среднее (notified_at − detected_at): от подтверждения до отправки в WS, мс"
    )
    notify_delay_ms_p95: float | None
    alerts_per_episode: float | None


class SourceMetrics(BaseModel):
    """Последний замер воркера по источнику (из processing_stats)."""

    source_id: str
    source_name: str | None
    source_status: SourceStatus | None = None
    run_started_at: UtcDatetime | None = Field(None, description="Начало запуска, к которому замер")
    measurement_state: Literal["not_started", "pending", "running", "stale", "finished"] = Field(
        "not_started",
        description="not_started — анализ не запускался; pending — запущен, замера ещё нет; "
        "running — свежий замер идущего анализа; stale — замер устарел (heartbeat не приходит "
        "или замер от прошлого запуска); finished — анализ завершён, это его последний замер",
    )
    ts: UtcDatetime | None
    fps: float | None = Field(description="Фактическая частота обработки, кадр/с")
    inference_ms_avg: float | None
    inference_ms_p95: float | None
    preprocess_ms_avg: float | None = Field(None, description="Подготовка входа сети, мс")
    postprocess_ms_avg: float | None = Field(None, description="Декодирование выхода и NMS, мс")
    scene_ms_avg: float | None = Field(
        None, description="Анализ сцены (модель фона по зонам), мс на обновление; null — не нужен"
    )
    frames_processed: int | None
    frame_ms_avg: float | None = Field(
        None, description="Полное время обработки кадра (для replay — скорость воспроизведения)"
    )
    recorded_inference_ms_avg: float | None = Field(
        None, description="Replay: историческое время инференса из записи (на LinQ), мс"
    )
    recorded_inference_ms_p50: float | None = None
    recorded_inference_ms_p95: float | None = None
    frames_unanalyzed: int | None = Field(None, description="Replay: кадров без записи")
    replay_file: str | None = None
    detector_kind: str | None = Field(
        description="mock | onnx_cpu (локальный CPU, не LinQ) | linq_tpu | <вид>_replay "
        "(предзаписанные результаты, например linq_tpu_replay)"
    )
    model_version: str | None
    latency: SourceLatency | None = None


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
