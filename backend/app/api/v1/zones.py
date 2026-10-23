"""Зоны на кадре источника."""

from fastapi import APIRouter, Response, status

from app.api.deps import DbSession
from app.schemas.sources import ZoneCreate, ZoneOut, ZoneUpdate
from app.services import zone_service

router = APIRouter(tags=["zones"])


@router.get("/sources/{source_id}/zones", response_model=list[ZoneOut])
def list_zones(source_id: str, db: DbSession) -> list[ZoneOut]:
    return [ZoneOut.model_validate(z) for z in zone_service.list_zones(db, source_id)]


@router.post(
    "/sources/{source_id}/zones", response_model=ZoneOut, status_code=status.HTTP_201_CREATED
)
def create_zone(source_id: str, body: ZoneCreate, db: DbSession) -> ZoneOut:
    return ZoneOut.model_validate(zone_service.create_zone(db, source_id, body))


@router.patch("/zones/{zone_id}", response_model=ZoneOut)
def update_zone(zone_id: str, body: ZoneUpdate, db: DbSession) -> ZoneOut:
    return ZoneOut.model_validate(zone_service.update_zone(db, zone_id, body))


@router.delete("/zones/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_zone(zone_id: str, db: DbSession) -> Response:
    zone_service.delete_zone(db, zone_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
