"""Разметка камеры одной операцией: все зоны (контур, название, назначение, активность) и правила
на весь кадр. Сначала проверяется весь черновик, затем изменения применяются в одной транзакции —
либо всё, либо ничего. Запущенный анализ подхватывает изменения сам (воркер перечитывает
конфигурацию раз в CONFIG_RELOAD_S, без перезапуска видео).
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import UnprocessableError
from app.models import Rule, Zone
from app.pipeline.geometry import polygon_problems
from app.pipeline.scenarios import get_scenario, list_scenarios
from app.schemas.layout import LayoutIn
from app.services import zone_service
from app.services.rule_service import _default_rule, _validate_params
from app.services.source_service import get_source


def _check(session: Session, source_id: str, body: LayoutIn) -> list[dict[str, Any]]:
    """Все ошибки черновика сразу: [{zone_index | frame_rule_index, field, message}]."""
    errors: list[dict[str, Any]] = []
    known = {str(c.scenario_type): c for c in list_scenarios()}
    own = set(session.scalars(select(Zone.id).where(Zone.source_id == source_id)))
    seen_ids = set()
    for i, zone in enumerate(body.zones):
        where = {"zone_index": i, "zone_id": zone.id}
        if zone.id is not None:
            if zone.id not in own:
                errors.append({**where, "field": "id", "message": "зона не принадлежит камере"})
            elif zone.id in seen_ids:
                errors.append({**where, "field": "id", "message": "зона указана дважды"})
            seen_ids.add(zone.id)
        if not zone.name.strip():
            errors.append({**where, "field": "name", "message": "введите название зоны"})
        for problem in polygon_problems([list(p) for p in zone.polygon]):
            errors.append({**where, "field": "polygon", "message": problem})
        if not zone.scenario_types:
            errors.append({**where, "field": "scenario_types", "message": "выберите правило"})
        for key in zone.scenario_types:
            cls = known.get(key)
            if cls is None:
                errors.append({**where, "field": "scenario_types",
                               "message": f"сценарий «{key}» не зарегистрирован"})  # fmt: skip
            elif not cls.requires_zone:
                message = f"«{cls.title}» работает на весь кадр, не в зоне"
                errors.append({**where, "field": "scenario_types", "message": message})
    for i, rule in enumerate(body.frame_rules):
        cls = known.get(rule.scenario_type)
        where = {"frame_rule_index": i, "field": "scenario_type"}
        if cls is None:
            errors.append(
                {**where, "message": f"сценарий «{rule.scenario_type}» не зарегистрирован"}
            )
        elif cls.requires_zone:
            errors.append({**where, "message": f"«{cls.title}» требует зону"})
    return errors


def _set_rule(rule: Rule, enabled: bool, params: dict[str, Any] | None) -> None:
    bump = False
    if rule.enabled != enabled:
        rule.enabled, bump = enabled, True
    if params is not None:
        cls = get_scenario(rule.scenario_type)
        stored = {k: v for k, v in rule.params.items() if k in cls.ParamsModel.model_fields}
        merged = _validate_params(cls, {**stored, **params})
        if merged != rule.params:
            rule.params, bump = merged, True
    if bump:
        rule.version += 1


def apply_layout(
    session: Session, settings: Settings, source_id: str, body: LayoutIn
) -> tuple[list[Zone], list[Rule]]:
    get_source(session, source_id)
    errors = _check(session, source_id, body)
    if errors:
        raise UnprocessableError(
            "Разметка не сохранена: " + "; ".join(e["message"] for e in errors[:3]),
            code="INVALID_LAYOUT",
            errors=errors,
        )
    removed_refs = []
    try:
        existing = {
            z.id: z for z in session.scalars(select(Zone).where(Zone.source_id == source_id))
        }
        keep = {z.id for z in body.zones if z.id}
        for zone_id, zone in existing.items():
            if zone_id not in keep:
                removed_refs.append(zone)
                session.delete(zone)  # правила зоны удаляет каскад
        session.flush()
        for item in body.zones:
            zone = existing.get(item.id) if item.id else None
            if zone is None:
                zone = Zone(source_id=source_id, name=item.name.strip(), polygon=[])
                session.add(zone)
            zone.name = item.name.strip()
            zone.polygon = [list(p) for p in item.polygon]
            session.flush()
            rules = {
                r.scenario_type: r
                for r in session.scalars(select(Rule).where(Rule.zone_id == zone.id))
            }
            for key, rule in rules.items():
                if key not in item.scenario_types:
                    session.delete(rule)
            for key in item.scenario_types:
                rule = rules.get(key)
                if rule is None:
                    rule = _default_rule(source_id, zone.id, get_scenario(key), auto=False)
                    session.add(rule)
                _set_rule(rule, item.enabled, None)
        frame = {
            r.scenario_type: r
            for r in session.scalars(
                select(Rule).where(Rule.source_id == source_id, Rule.zone_id.is_(None))
            )
        }
        for item in body.frame_rules:
            rule = frame.get(item.scenario_type)
            if rule is None:
                rule = _default_rule(source_id, None, get_scenario(item.scenario_type), auto=False)
                session.add(rule)
            _set_rule(rule, item.enabled, item.params)
        session.commit()
    except Exception:
        session.rollback()
        raise
    for zone in removed_refs:
        zone_service.remove_reference_file(settings, zone)
    zones = list(
        session.scalars(select(Zone).where(Zone.source_id == source_id).order_by(Zone.created_at))
    )
    rules = list(
        session.scalars(select(Rule).where(Rule.source_id == source_id).order_by(Rule.created_at))
    )
    return zones, rules
