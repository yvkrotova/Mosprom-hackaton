"""Журнал событий: список с фильтрами, карточка события, действия оператора."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbSession, HubDep
from app.errors import UnprocessableError
from app.models.base import as_utc
from app.schemas.enums import EpisodeState, Priority, ReviewStatus, ScenarioType
from app.schemas.events import EventDetail, EventPage, OperatorActionCreate
from app.services import event_service
from app.services.event_service import EventFilters

router = APIRouter(tags=["events"])


@router.get("/events", response_model=EventPage, summary="Журнал событий")
def list_events(
    db: DbSession,
    scenario_type: ScenarioType | None = None,
    source_id: str | None = None,
    zone_id: str | None = None,
    from_: Annotated[
        datetime | None,
        Query(alias="from", description="Начало интервала (эпизод пересекается с интервалом)"),
    ] = None,
    to: Annotated[datetime | None, Query(description="Конец интервала")] = None,
    state: EpisodeState | None = None,
    review_status: ReviewStatus | None = None,
    priority: Priority | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventPage:
    from_, to = as_utc(from_), as_utc(to)  # время без пояса — UTC
    if from_ and to and from_ > to:
        raise UnprocessableError(
            "Параметр from позже to", **{"from": from_.isoformat(), "to": to.isoformat()}
        )
    filters = EventFilters(
        scenario_type=scenario_type,
        source_id=source_id,
        zone_id=zone_id,
        from_=from_,
        to=to,
        state=state,
        review_status=review_status,
        priority=priority,
    )
    return event_service.list_events(db, filters, limit, offset)


@router.get("/events/{event_id}", response_model=EventDetail, summary="Карточка события")
def get_event(event_id: str, db: DbSession) -> EventDetail:
    return event_service.event_to_detail(db, event_service.get_event(db, event_id))


@router.post(
    "/events/{event_id}/actions",
    response_model=EventDetail,
    summary="Действие оператора (подтвердить, ложное, эскалировать, комментарий, сброс)",
)
def add_action(
    event_id: str, body: OperatorActionCreate, db: DbSession, hub: HubDep
) -> EventDetail:
    return event_service.apply_action(db, hub, event_id, body)
