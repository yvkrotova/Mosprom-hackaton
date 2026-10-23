"""Сводка журнала, метрики производительности и здоровье системы."""

import logging
from collections import Counter
from datetime import datetime

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Event, ProcessingStat, Source, Zone
from app.models.base import as_utc
from app.schemas.enums import ReviewStatus, SourceStatus
from app.schemas.stats import (
    CountItem,
    HealthOut,
    HourCount,
    LatencyMetrics,
    MetricsOut,
    ReviewCounts,
    SourceHealth,
    SourceMetrics,
    StatsOut,
)
from app.services.event_service import interval_conditions

logger = logging.getLogger(__name__)


def _counts(session: Session, column, conds, labels: dict[str, str] | None = None):
    rows = session.execute(
        select(column, func.count()).where(*conds, column.is_not(None)).group_by(column)
    ).all()
    items = [
        CountItem(key=str(key), label=(labels or {}).get(key), count=count) for key, count in rows
    ]
    return sorted(items, key=lambda i: (-i.count, i.key))


def get_stats(
    session: Session, from_: datetime | None, to: datetime | None, source_id: str | None = None
) -> StatsOut:
    conds = interval_conditions(from_, to)
    if source_id:
        conds.append(Event.source_id == source_id)
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


def get_metrics(session: Session, source_id: str | None) -> MetricsOut:
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
        items.append(
            SourceMetrics(
                source_id=source.id,
                source_name=source.name,
                ts=stat.ts if stat else None,
                fps=stat.fps if stat else None,
                inference_ms_avg=stat.inference_ms_avg if stat else None,
                inference_ms_p95=stat.inference_ms_p95 if stat else None,
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
