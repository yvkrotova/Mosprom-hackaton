"""Сводка для дашборда, замеры скорости для отчёта, здоровье системы."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.deps import DbSession, SettingsDep
from app.errors import UnprocessableError
from app.models.base import as_utc
from app.pipeline.detectors import detector_kind_for
from app.schemas.stats import HealthOut, MetricsOut, StatsOut
from app.services import stats_service

router = APIRouter(tags=["stats"])


@router.get(
    "/stats",
    response_model=StatsOut,
    response_model_by_alias=True,
    summary="Количество событий по типам, камерам, зонам, часам и результатам проверки",
)
def get_stats(
    db: DbSession,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    source_id: Annotated[str | None, Query(description="Только одна камера")] = None,
) -> StatsOut:
    from_, to = as_utc(from_), as_utc(to)  # время без пояса — UTC
    if from_ and to and from_ > to:
        raise UnprocessableError(
            "Параметр from позже to", **{"from": from_.isoformat(), "to": to.isoformat()}
        )
    return stats_service.get_stats(db, from_, to, source_id)


@router.get(
    "/metrics",
    response_model=MetricsOut,
    summary="FPS, время инференса, задержки обнаружения и оповещения",
)
def get_metrics(db: DbSession, source_id: str | None = None) -> MetricsOut:
    return stats_service.get_metrics(db, source_id)


@router.get("/health", response_model=HealthOut, summary="Состояние БД и источников")
def get_health(request: Request, settings: SettingsDep) -> HealthOut:
    return stats_service.get_health(
        request.app.state.session_factory, detector_kind_for(settings.detector)
    )
