"""Журнал событий: список с фильтрами, карточка события, действия оператора."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import DbSession, HubDep
from app.errors import UnprocessableError
from app.models.base import as_utc
from app.schemas.enums import EpisodeState, Priority, ReviewStatus, ScenarioType
from app.schemas.events import (
    EventDetail,
    EventOut,
    EventPage,
    OperatorActionCreate,
    UnviewedCounts,
)
from app.services import event_service, rule_service
from app.services.event_service import EventFilters

router = APIRouter(tags=["events"])


def event_filters(
    scenario_type: ScenarioType | None = None,
    source_id: str | None = None,
    zone_id: Annotated[
        str | None, Query(description='id зоны или "none" — только события «на весь кадр»')
    ] = None,
    from_: Annotated[
        datetime | None, Query(alias="from", description="Начало эпизода не раньше (включительно)")
    ] = None,
    to: Annotated[
        datetime | None, Query(description="Начало эпизода не позже (включительно)")
    ] = None,
    state: EpisodeState | None = None,
    review_status: Annotated[
        ReviewStatus | None,
        Query(description="confirmed включает escalated (передано мастеру = подтверждено)"),
    ] = None,
    priority: Priority | None = None,
    site: Annotated[str | None, Query(description="Участок камеры")] = None,
    viewed: Annotated[bool | None, Query(description="Оператор открывал событие")] = None,
    order: Annotated[Literal["desc", "asc"], Query(description="По началу эпизода")] = "desc",
) -> EventFilters:
    from_, to = as_utc(from_), as_utc(to)  # время без пояса — UTC
    if from_ and to and from_ > to:
        raise UnprocessableError(
            "Параметр from позже to", **{"from": from_.isoformat(), "to": to.isoformat()}
        )
    return EventFilters(
        scenario_type=scenario_type,
        source_id=source_id,
        zone_id=zone_id,
        from_=from_,
        to=to,
        state=state,
        review_status=review_status,
        priority=priority,
        site=site,
        viewed=viewed,
        order=order,
    )


Filters = Annotated[EventFilters, Depends(event_filters)]


@router.get("/events", response_model=EventPage, summary="Журнал событий")
def list_events(
    db: DbSession,
    filters: Filters,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventPage:
    return event_service.list_events(db, filters, limit, offset)


@router.get(
    "/events/export.csv",
    summary="Выгрузка журнала в CSV (вся выборка с фильтрами и сортировкой)",
    response_class=Response,
)
def export_events(
    db: DbSession,
    filters: Filters,
    tz_offset_min: Annotated[
        int, Query(ge=-14 * 60, le=14 * 60, description="Смещение местного времени от UTC, мин")
    ] = 0,
) -> Response:
    titles = {str(s.scenario_type): s.title for s in rule_service.scenario_infos()}
    body = event_service.export_csv(db, filters, tz_offset_min, titles)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="journal.csv"'},
    )


@router.get(
    "/events/unviewed", response_model=UnviewedCounts, summary="Непросмотренные: всего и по камерам"
)
def unviewed(db: DbSession, site: str | None = None) -> UnviewedCounts:
    return UnviewedCounts.model_validate(event_service.unviewed_counts(db, site))


@router.get("/events/{event_id}", response_model=EventDetail, summary="Карточка события")
def get_event(event_id: str, db: DbSession) -> EventDetail:
    return event_service.event_to_detail(db, event_service.get_event(db, event_id))


@router.post(
    "/events/{event_id}/view",
    response_model=EventOut,
    summary="Отметить событие просмотренным (идемпотентно, проверку не меняет)",
)
def view_event(event_id: str, db: DbSession, hub: HubDep) -> EventOut:
    return event_service.mark_viewed(db, hub, event_id)


@router.post(
    "/events/{event_id}/actions",
    response_model=EventDetail,
    summary="Действие оператора (подтвердить, ложное, эскалировать, комментарий, сброс)",
)
def add_action(
    event_id: str, body: OperatorActionCreate, db: DbSession, hub: HubDep
) -> EventDetail:
    return event_service.apply_action(db, hub, event_id, body)
