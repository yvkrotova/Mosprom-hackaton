"""Правила (экземпляры сценариев, привязанные к источнику и зоне) и описание сценариев."""

from typing import Any

from pydantic import Field

from app.schemas.common import ApiModel, InputModel, UtcDatetime, UuidStr
from app.schemas.enums import Priority, ScenarioType


class RuleCreate(InputModel):
    source_id: UuidStr
    zone_id: UuidStr | None = Field(
        None, description="Зона правила; null — правило применяется ко всем зонам источника"
    )
    # Строка, а не enum: неизвестный сценарий проверяется по реестру и даёт 422 UNKNOWN_SCENARIO.
    scenario_type: str = Field(examples=["danger_zone"])
    enabled: bool = True
    priority: Priority = Priority.HIGH
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Параметры сценария; не указанные берутся из дефолтов (см. GET /scenarios)",
    )


class RuleUpdate(InputModel):
    zone_id: UuidStr | None = None
    enabled: bool | None = None
    priority: Priority | None = None
    params: dict[str, Any] | None = None


class RuleOut(ApiModel):
    id: str
    source_id: str
    zone_id: str | None
    scenario_type: ScenarioType
    enabled: bool
    priority: Priority
    params: dict[str, Any] = Field(description="Полные параметры с подставленными дефолтами")
    version: int = Field(description="Увеличивается при каждом изменении params или enabled")
    auto_created: bool = Field(
        False, description="Создано автоматически для сценария с auto_enable («по умолчанию»)"
    )
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ScenarioInfo(ApiModel):
    scenario_type: ScenarioType
    title: str
    description: str
    requires_zone: bool
    auto_enable: bool = Field(
        False, description="Правило на весь кадр создаётся само для каждого источника"
    )
    params_schema: dict[str, Any] = Field(description="JSON Schema параметров")
    defaults: dict[str, Any]
