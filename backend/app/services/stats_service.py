"""Сводка журнала, метрики производительности и здоровье системы."""

import logging
from collections import Counter
from datetime import datetime

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Event, ProcessingStat, Source, Zone
from app.models.base import as_utc, utcnow
from app.schemas.enums import EpisodeState, ReviewStatus, ScenarioType, SourceStatus
from app.schemas.stats import (
    CountItem,
    DemoFlag,
    HealthOut,
    HourCount,
    LatencyMetrics,
    MetricsOut,
    ReviewCounts,
    SourceHealth,
    SourceLatency,
    SourceMetrics,
    StatsOut,
    StatsSummary,
)
from app.services.event_service import CONFIRMED_STATUSES, interval_conditions

# Замер «устарел», если от воркера нет heartbeat дольше стольких интервалов.
STALE_AFTER_HEARTBEATS = 5

logger = logging.getLogger(__name__)


def _counts(session: Session, column, conds, labels: dict[str, str] | None = None):
    rows = session.execute(
        select(column, func.count()).where(*conds, column.is_not(None)).group_by(column)
    ).all()
    items = [
        CountItem(key=str(key), label=(labels or {}).get(key), count=count) for key, count in rows
    ]
    return sorted(items, key=lambda i: (-i.count, i.key))


def _summary(session: Session, conds) -> StatsSummary:
    def count(*extra) -> int:
        return session.scalar(select(func.count()).select_from(Event).where(*conds, *extra)) or 0

    total = count()
    zone_times = session.scalars(
        select(Event.video_ts_end - Event.video_ts_start).where(
            *conds,
            Event.scenario_type == ScenarioType.DANGER_ZONE,
            Event.state == EpisodeState.ENDED,
        )
    ).all()
    alerts = session.scalar(select(func.sum(Event.alerts_sent)).where(*conds)) or 0
    return StatsSummary(
        total=total,
        active=count(Event.state == EpisodeState.ACTIVE),
        unreviewed=count(Event.review_status == ReviewStatus.UNREVIEWED),
        confirmed=count(Event.review_status.in_(CONFIRMED_STATUSES)),
        escalated=count(Event.review_status == ReviewStatus.ESCALATED),
        false_positive=count(Event.review_status == ReviewStatus.FALSE_POSITIVE),
        avg_zone_time_s=round(float(np.mean(zone_times)), 1) if zone_times else None,
        alerts_per_episode=round(alerts / total, 2) if total else None,
    )


def _demo(session: Session, conds, source_id: str | None) -> DemoFlag:
    """Демо — события от заглушки детектора (mock) или источники, работающие на mock."""
    mock_events = session.scalar(
        select(func.count()).select_from(Event).where(*conds, Event.model_version.like("mock%"))
    )
    if mock_events:
        return DemoFlag(is_demo=True, reason="события получены заглушкой детектора (mock)")
    history = session.scalar(
        select(func.count()).select_from(Event).where(*conds, Event.model_version == "demo-history")
    )
    if history:
        return DemoFlag(is_demo=True, reason="в периоде есть сгенерированная демо-история")
    q = select(ProcessingStat.detector_kind).where(ProcessingStat.detector_kind == "mock")
    if source_id:
        q = q.where(ProcessingStat.source_id == source_id)
    if session.scalar(q.limit(1)):
        return DemoFlag(is_demo=True, reason="камера анализировалась заглушкой детектора (mock)")
    return DemoFlag(is_demo=False)


def get_stats(
    session: Session,
    from_: datetime | None,
    to: datetime | None,
    source_id: str | None = None,
    site: str | None = None,
) -> StatsOut:
    conds = interval_conditions(from_, to)
    if source_id:
        conds.append(Event.source_id == source_id)
    if site:
        conds.append(Event.source_id.in_(select(Source.id).where(Source.site == site)))
    total = session.scalar(select(func.count()).select_from(Event).where(*conds)) or 0
    source_names = dict(session.execute(select(Source.id, Source.name)).all())
    zone_names = dict(session.execute(select(Zone.id, Zone.name)).all())

    # Часы считаем в Python: группировка по часу в SQL у SQLite и PostgreSQL разная.
    hours = Counter(
        as_utc(started).replace(minute=0, second=0, microsecond=0)
        for started in session.scalars(select(Event.started_at).where(*conds))
    )
    review = dict(
        session.execute(
            select(Event.review_status, func.count()).where(*conds).group_by(Event.review_status)
        ).all()
    )
    return StatsOut(
        from_=from_,
        to=to,
        summary=_summary(session, conds),
        demo=_demo(session, conds, source_id),
        total=total,
        by_scenario_type=_counts(session, Event.scenario_type, conds),
        by_source=_counts(session, Event.source_id, conds, source_names),
        by_zone=_counts(session, Event.zone_id, conds, zone_names),
        by_hour=[HourCount(hour=h, count=c) for h, c in sorted(hours.items())],
        review=ReviewCounts(**{s.value: review.get(s.value, 0) for s in ReviewStatus}),
    )


def _avg(values: list[float]) -> float | None:
    return round(float(np.mean(values)), 3) if values else None


def _p95(values: list[float]) -> float | None:
    return round(float(np.percentile(values, 95)), 3) if values else None


def _latency(events: list[Event]) -> SourceLatency:
    real = [(as_utc(e.detected_at) - as_utc(e.started_at)).total_seconds() for e in events]
    video = [e.video_ts_detected - e.video_ts_start for e in events]
    notify = [
        (as_utc(e.notified_at) - as_utc(e.detected_at)).total_seconds() * 1000
        for e in events
        if e.notified_at is not None
    ]
    return SourceLatency(
        events_count=len(events),
        detection_delay_video_ts_s_avg=_avg(video),
        detection_delay_video_ts_s_p95=_p95(video),
        detection_delay_real_s_avg=_avg(real),
        detection_delay_real_s_p95=_p95(real),
        notify_delay_ms_avg=_avg(notify),
        notify_delay_ms_p95=_p95(notify),
        alerts_per_episode=(
            round(sum(e.alerts_sent for e in events) / len(events), 3) if events else None
        ),
    )


def _measurement_state(source: Source, stat: ProcessingStat | None, stale_s: float) -> str:
    started = as_utc(source.run_started_at)
    if started is None and stat is None:
        return "not_started"
    fresh_run = stat is not None and (started is None or as_utc(stat.ts) >= started)
    if source.status in (SourceStatus.RUNNING, SourceStatus.DISCONNECTED):
        if not fresh_run:
            return "pending" if source.status == SourceStatus.RUNNING else "stale"
        age = (utcnow() - as_utc(stat.ts)).total_seconds()
        return "running" if age <= stale_s else "stale"
    if not fresh_run:
        return "stale" if stat is not None else "not_started"
    return "finished"


def get_metrics(
    session: Session, source_id: str | None, heartbeat_interval_s: float = 2.0
) -> MetricsOut:
    sources_q = select(Source).order_by(Source.created_at)
    if source_id:
        sources_q = sources_q.where(Source.id == source_id)

    items = []
    for source in session.scalars(sources_q):
        stat = session.scalars(
            select(ProcessingStat)
            .where(ProcessingStat.source_id == source.id)
            .order_by(ProcessingStat.ts.desc())
            .limit(1)
        ).first()
        started = as_utc(source.run_started_at)
        run_events = session.scalars(
            select(Event).where(
                Event.source_id == source.id,
                *([Event.detected_at >= started] if started else []),
            )
        ).all()
        items.append(
            SourceMetrics(
                source_id=source.id,
                source_name=source.name,
                source_status=source.status,
                run_started_at=started,
                measurement_state=_measurement_state(
                    source, stat, STALE_AFTER_HEARTBEATS * heartbeat_interval_s
                ),
                frame_ms_avg=stat.frame_ms_avg if stat else None,
                recorded_inference_ms_avg=stat.recorded_inference_ms_avg if stat else None,
                recorded_inference_ms_p50=stat.recorded_inference_ms_p50 if stat else None,
                recorded_inference_ms_p95=stat.recorded_inference_ms_p95 if stat else None,
                frames_unanalyzed=stat.frames_unanalyzed if stat else None,
                replay_file=stat.replay_file if stat else None,
                latency=_latency(run_events) if started else None,
                ts=stat.ts if stat else None,
                fps=stat.fps if stat else None,
                inference_ms_avg=stat.inference_ms_avg if stat else None,
                inference_ms_p95=stat.inference_ms_p95 if stat else None,
                preprocess_ms_avg=stat.preprocess_ms_avg if stat else None,
                postprocess_ms_avg=stat.postprocess_ms_avg if stat else None,
                scene_ms_avg=stat.scene_ms_avg if stat else None,
                frames_processed=stat.frames_processed if stat else None,
                detector_kind=stat.detector_kind if stat else None,
                model_version=stat.model_version if stat else None,
            )
        )

    events_q = select(Event)
    if source_id:
        events_q = events_q.where(Event.source_id == source_id)
    events = session.scalars(events_q).all()

    real = [(as_utc(e.detected_at) - as_utc(e.started_at)).total_seconds() for e in events]
    video = [e.video_ts_detected - e.video_ts_start for e in events]
    notify = [
        (as_utc(e.notified_at) - as_utc(e.detected_at)).total_seconds() * 1000
        for e in events
        if e.notified_at is not None
    ]
    latency = LatencyMetrics(
        events_count=len(events),
        detection_delay_real_s_avg=_avg(real),
        detection_delay_real_s_p95=_p95(real),
        detection_delay_video_ts_s_avg=_avg(video),
        detection_delay_video_ts_s_p95=_p95(video),
        notify_delay_ms_avg=_avg(notify),
        notify_delay_ms_p95=_p95(notify),
        alerts_per_episode=(
            round(sum(e.alerts_sent for e in events) / len(events), 3) if events else None
        ),
    )
    return MetricsOut(sources=items, latency=latency)


_UNHEALTHY = {SourceStatus.ERROR, SourceStatus.DISCONNECTED}


def get_health(session_factory: sessionmaker[Session], detector_kind: str) -> HealthOut:
    """Не бросает исключений: недоступная БД — это db=error, а не 500."""
    try:
        with session_factory() as session:
            session.execute(select(1))
            sources = session.scalars(select(Source).order_by(Source.created_at)).all()
            source_health = [SourceHealth.model_validate(s) for s in sources]
        db = "ok"
    except Exception:
        logger.exception("Проверка БД не прошла")
        db, source_health = "error", []
    degraded = db == "error" or any(s.status in _UNHEALTHY for s in source_health)
    return HealthOut(
        status="degraded" if degraded else "ok",
        db=db,
        detector_kind=detector_kind,
        sources=source_health,
    )
