"""Правила анализа и список доступных сценариев."""

from fastapi import APIRouter, Response, status

from app.api.deps import DbSession
from app.schemas.rules import RuleCreate, RuleOut, RuleUpdate, ScenarioInfo
from app.services import rule_service

router = APIRouter(tags=["rules"])


@router.get("/scenarios", response_model=list[ScenarioInfo], summary="Зарегистрированные сценарии")
def list_scenarios() -> list[ScenarioInfo]:
    return rule_service.scenario_infos()


@router.get("/rules", response_model=list[RuleOut])
def list_rules(db: DbSession, source_id: str | None = None) -> list[RuleOut]:
    return [RuleOut.model_validate(r) for r in rule_service.list_rules(db, source_id)]


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(body: RuleCreate, db: DbSession) -> RuleOut:
    return RuleOut.model_validate(rule_service.create_rule(db, body))


@router.patch("/rules/{rule_id}", response_model=RuleOut)
def update_rule(rule_id: str, body: RuleUpdate, db: DbSession) -> RuleOut:
    return RuleOut.model_validate(rule_service.update_rule(db, rule_id, body))


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: str, db: DbSession) -> Response:
    rule_service.delete_rule(db, rule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
