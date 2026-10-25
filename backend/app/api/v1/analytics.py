"""Аналитика «Статистики» и генератор демо-истории."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession
from app.errors import UnprocessableError
from app.services import analytics_service, demo_service

router = APIRouter(tags=["analytics"])
PERIODS = (7, 14, 30)

TzOffset = Annotated[
    int, Query(ge=-14 * 60, le=14 * 60, description="Смещение местного времени от UTC, мин")
]


@router.get(
    "/analytics/overview",
    summary="Сводка, динамика по дням, рейтинг начальников смен, горячие точки — одним ответом",
)
def analytics_overview(
    db: DbSession,
    days: Annotated[int, Query(description="Период: последние 7, 14 или 30 дней")] = 14,
    supervisor: Annotated[
        str | None, Query(description="Динамика только по этому начальнику")
    ] = None,
    tz_offset_min: TzOffset = 0,
) -> dict:
    if days not in PERIODS:
        raise UnprocessableError("Период — 7, 14 или 30 дней", code="BAD_PERIOD", days=days)
    return analytics_service.overview(db, days, supervisor or None, tz_offset_min)


@router.post(
    "/demo/history",
    summary="Сгенерировать демо-историю (начальники смен, смены, инциденты) — заменяет прежнюю",
)
def generate_demo(
    db: DbSession,
    days: Annotated[int, Query(ge=7, le=60)] = 30,
    tz_offset_min: TzOffset = 180,
) -> dict:
    return demo_service.generate(db, days, tz_offset_min)


@router.delete("/demo/history", summary="Удалить демо-историю (реальные данные не трогаются)")
def clear_demo(db: DbSession) -> dict:
    return demo_service.clear(db)
