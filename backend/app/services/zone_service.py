"""Зоны (полигоны) на кадре источника."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models import Zone
from app.schemas.sources import ZoneCreate, ZoneUpdate
from app.services.source_service import get_source


def get_zone(session: Session, zone_id: str) -> Zone:
    zone = session.get(Zone, zone_id)
    if zone is None:
        raise NotFoundError("Зона не найдена", code="ZONE_NOT_FOUND", zone_id=zone_id)
    return zone


def list_zones(session: Session, source_id: str) -> list[Zone]:
    get_source(session, source_id)
    return list(
        session.scalars(select(Zone).where(Zone.source_id == source_id).order_by(Zone.created_at))
    )


def create_zone(session: Session, source_id: str, body: ZoneCreate) -> Zone:
    get_source(session, source_id)
    zone = Zone(source_id=source_id, name=body.name, polygon=[list(p) for p in body.polygon])
    session.add(zone)
    session.commit()
    return zone


def update_zone(session: Session, zone_id: str, body: ZoneUpdate) -> Zone:
    zone = get_zone(session, zone_id)
    if body.name is not None:
        zone.name = body.name
    if body.polygon is not None:
        zone.polygon = [list(p) for p in body.polygon]
    session.commit()
    return zone


def delete_zone(session: Session, zone_id: str) -> None:
    session.delete(get_zone(session, zone_id))  # правила зоны удаляет ON DELETE CASCADE
    session.commit()
