"""Правила: экземпляры сценариев с параметрами, проверенными схемой сценария из реестра."""

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFoundError, UnprocessableError
from app.models import Rule, Zone
from app.pipeline.interfaces import ScenarioRule
from app.pipeline.scenarios import get_scenario, list_scenarios
from app.schemas.rules import RuleCreate, RuleUpdate, ScenarioInfo
from app.services.source_service import get_source
from app.services.zone_service import get_zone


def scenario_infos() -> list[ScenarioInfo]:
    return [
        ScenarioInfo(
            scenario_type=cls.scenario_type,
            title=cls.title,
            description=cls.description,
            requires_zone=cls.requires_zone,
            params_schema=cls.ParamsModel.model_json_schema(),
            defaults=cls.ParamsModel().model_dump(mode="json"),
        )
        for cls in list_scenarios()
    ]


def _scenario(scenario_type: str) -> type[ScenarioRule]:
    try:
        return get_scenario(scenario_type)
    except KeyError:
        raise UnprocessableError(
            f"Сценарий «{scenario_type}» не зарегистрирован",
            code="UNKNOWN_SCENARIO",
            scenario_type=scenario_type,
            available=[str(cls.scenario_type) for cls in list_scenarios()],
        ) from None


def _validate_params(scenario: type[ScenarioRule], params: dict[str, Any]) -> dict[str, Any]:
    """Полные параметры с дефолтами или 422 INVALID_SCENARIO_PARAMS."""
    try:
        return scenario.ParamsModel.model_validate(params).model_dump(mode="json")
    except ValidationError as exc:
        raise UnprocessableError(
            "Параметры не подходят сценарию",
            code="INVALID_SCENARIO_PARAMS",
            errors=[
                {"loc": ["params", *e["loc"]], "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ],
        ) from None


def _check_zone(session: Session, zone_id: str, source_id: str) -> None:
    zone = get_zone(session, zone_id)
    if zone.source_id != source_id:
        raise UnprocessableError(
            "Зона принадлежит другому источнику",
            code="ZONE_SOURCE_MISMATCH",
            zone_id=zone_id,
            source_id=source_id,
        )


def get_rule(session: Session, rule_id: str) -> Rule:
    rule = session.get(Rule, rule_id)
    if rule is None:
        raise NotFoundError("Правило не найдено", code="RULE_NOT_FOUND", rule_id=rule_id)
    return rule


def list_rules(session: Session, source_id: str | None) -> list[Rule]:
    query = select(Rule).order_by(Rule.created_at)
    if source_id:
        query = query.where(Rule.source_id == source_id)
    return list(session.scalars(query))


def create_rule(session: Session, body: RuleCreate) -> Rule:
    scenario = _scenario(body.scenario_type)
    get_source(session, body.source_id)
    if body.zone_id:
        _check_zone(session, body.zone_id, body.source_id)
    elif scenario.requires_zone:
        has_zones = session.scalar(select(Zone.id).where(Zone.source_id == body.source_id))
        if not has_zones:
            raise UnprocessableError(
                "Сценарию нужна зона, а у источника нет ни одной",
                code="ZONE_REQUIRED",
                source_id=body.source_id,
            )
    rule = Rule(
        source_id=body.source_id,
        zone_id=body.zone_id,
        scenario_type=str(scenario.scenario_type),
        enabled=body.enabled,
        priority=body.priority,
        params=_validate_params(scenario, body.params),
        version=1,
    )
    session.add(rule)
    session.commit()
    return rule


def update_rule(session: Session, rule_id: str, body: RuleUpdate) -> Rule:
    rule = get_rule(session, rule_id)
    scenario = _scenario(rule.scenario_type)
    sent = body.model_fields_set

    if "zone_id" in sent:
        if body.zone_id:
            _check_zone(session, body.zone_id, rule.source_id)
        rule.zone_id = body.zone_id  # null — все зоны источника
    if body.priority is not None:
        rule.priority = body.priority

    bump = False
    if body.enabled is not None and body.enabled != rule.enabled:
        rule.enabled = body.enabled
        bump = True
    if body.params is not None:
        params = _validate_params(scenario, {**rule.params, **body.params})
        if params != rule.params:
            rule.params = params
            bump = True
    if bump:
        rule.version += 1  # в событии сохраняется версия правила, по которой оно создано
    session.commit()
    return rule


def delete_rule(session: Session, rule_id: str) -> None:
    session.delete(get_rule(session, rule_id))
    session.commit()
