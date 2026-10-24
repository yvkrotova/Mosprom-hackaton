"""Зоны на кадре источника."""

from fastapi import APIRouter, Response, status

from app.api.deps import DbSession
from app.models import Zone
from app.schemas.sources import ZoneCreate, ZoneOut, ZoneUpdate
from app.services import rule_service, zone_service

router = APIRouter(tags=["zones"])


def _out(db, zone: Zone) -> ZoneOut:
    out = ZoneOut.model_validate(zone)
    out.scenario_types = rule_service.zone_scenarios(db, zone.id)
    return out


@router.get("/sources/{source_id}/zones", response_model=list[ZoneOut])
def list_zones(source_id: str, db: DbSession) -> list[ZoneOut]:
    return [_out(db, z) for z in zone_service.list_zones(db, source_id)]


@router.post(
    "/sources/{source_id}/zones", response_model=ZoneOut, status_code=status.HTTP_201_CREATED
)
def create_zone(source_id: str, body: ZoneCreate, db: DbSession) -> ZoneOut:
    zone = zone_service.create_zone(db, source_id, body)
    try:
        rule_service.sync_zone_rules(db, zone, body.scenario_types, creating=True)
    except Exception:
        zone_service.delete_zone(db, zone.id)  # неверный сценарий — зона не создаётся
        raise
    return _out(db, zone)


@router.patch("/zones/{zone_id}", response_model=ZoneOut)
def update_zone(zone_id: str, body: ZoneUpdate, db: DbSession) -> ZoneOut:
    zone = zone_service.update_zone(db, zone_id, body)
    rule_service.sync_zone_rules(db, zone, body.scenario_types, creating=False)
    return _out(db, zone)


@router.delete("/zones/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_zone(zone_id: str, db: DbSession) -> Response:
    zone_service.delete_zone(db, zone_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
