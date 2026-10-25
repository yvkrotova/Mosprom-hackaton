"""Смены руководителей и рейтинг безопасности; список экстренных служб."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import DbSession, SettingsDep
from app.errors import UnprocessableError
from app.models.base import as_utc
from app.schemas.shifts import (
    EmergencyContact,
    ShiftCreate,
    ShiftOut,
    ShiftUpdate,
    SupervisorRating,
)
from app.services import shift_service

router = APIRouter(tags=["shifts"])


def _period(from_: datetime | None, to: datetime | None) -> tuple[datetime | None, datetime | None]:
    from_, to = as_utc(from_), as_utc(to)
    if from_ and to and from_ > to:
        raise UnprocessableError(
            "Параметр from позже to", **{"from": from_.isoformat(), "to": to.isoformat()}
        )
    return from_, to


@router.get("/shifts", response_model=list[ShiftOut], summary="Смены, пересекающиеся с периодом")
def list_shifts(
    db: DbSession,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> list[ShiftOut]:
    return [ShiftOut.model_validate(s) for s in shift_service.list_shifts(db, *_period(from_, to))]


@router.post("/shifts", response_model=ShiftOut, status_code=status.HTTP_201_CREATED)
def create_shift(body: ShiftCreate, db: DbSession) -> ShiftOut:
    return ShiftOut.model_validate(shift_service.create_shift(db, body))


@router.patch("/shifts/{shift_id}", response_model=ShiftOut)
def update_shift(shift_id: str, body: ShiftUpdate, db: DbSession) -> ShiftOut:
    return ShiftOut.model_validate(shift_service.update_shift(db, shift_id, body))


@router.post("/shifts/{shift_id}/end", response_model=ShiftOut, summary="Завершить смену сейчас")
def end_shift(shift_id: str, db: DbSession) -> ShiftOut:
    return ShiftOut.model_validate(shift_service.end_shift(db, shift_id))


@router.delete("/shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift(shift_id: str, db: DbSession) -> Response:
    shift_service.delete_shift(db, shift_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/shifts/rating",
    response_model=list[SupervisorRating],
    summary="Рейтинг руководителей: опасных ситуаций на 8 часов смены (меньше — выше)",
)
def shift_rating(
    db: DbSession,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    site: Annotated[str | None, Query(description="Только участок")] = None,
) -> list[SupervisorRating]:
    return [SupervisorRating(**r) for r in shift_service.rating(db, *_period(from_, to), site)]


@router.get(
    "/emergency/contacts", response_model=list[EmergencyContact], summary="Экстренные службы"
)
def emergency_contacts(settings: SettingsDep) -> list[EmergencyContact]:
    return [EmergencyContact(**c) for c in settings.emergency_contact_list]
