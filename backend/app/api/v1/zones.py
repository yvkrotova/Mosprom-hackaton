"""Зоны на кадре источника."""

from fastapi import APIRouter, Response, status
from sqlalchemy import select

from app.api.deps import DbSession, SettingsDep
from app.models import Rule, Zone
from app.schemas.layout import LayoutIn, LayoutOut
from app.schemas.rules import RuleOut
from app.schemas.sources import ZoneCreate, ZoneOut, ZoneUpdate
from app.services import layout_service, rule_service, zone_service

router = APIRouter(tags=["zones"])


def _out(db, zone: Zone) -> ZoneOut:
    out = ZoneOut.model_validate(zone)
    rules = list(db.scalars(select(Rule).where(Rule.zone_id == zone.id)))
    out.scenario_types = sorted(r.scenario_type for r in rules)
    out.enabled = bool(rules) and all(r.enabled for r in rules)
    return out


@router.put(
    "/sources/{source_id}/layout",
    response_model=LayoutOut,
    summary="Сохранить разметку камеры одной операцией (все зоны и правила на весь кадр)",
)
def put_layout(source_id: str, body: LayoutIn, db: DbSession, settings: SettingsDep) -> LayoutOut:
    zones, rules = layout_service.apply_layout(db, settings, source_id, body)
    return LayoutOut(
        zones=[_out(db, z) for z in zones], rules=[RuleOut.model_validate(r) for r in rules]
    )


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
def delete_zone(zone_id: str, db: DbSession, settings: SettingsDep) -> Response:
    zone_service.delete_zone(db, zone_id, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/zones/{zone_id}/reference",
    response_model=ZoneOut,
    summary="Запомнить пустой проход: текущий кадр источника — эталон зоны для анализа сцены",
)
def set_reference(zone_id: str, db: DbSession, settings: SettingsDep) -> ZoneOut:
    return _out(db, zone_service.set_reference(db, settings, zone_id))


@router.delete(
    "/zones/{zone_id}/reference",
    response_model=ZoneOut,
    summary="Забыть эталон: анализ сцены снова соберёт его по первым секундам видео",
)
def clear_reference(zone_id: str, db: DbSession, settings: SettingsDep) -> ZoneOut:
    return _out(db, zone_service.clear_reference(db, settings, zone_id))
