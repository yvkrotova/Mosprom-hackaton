"""Разметка камеры целиком (PUT /sources/{id}/layout): зоны и правила на весь кадр."""

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import InputModel, Point
from app.schemas.rules import RuleOut
from app.schemas.sources import ZoneOut


class LayoutZone(InputModel):
    id: str | None = Field(None, description="Существующая зона; null — новая")
    # Проверки контура и названия — общие для всего черновика (ошибки с индексом зоны),
    # поэтому здесь только типы: см. layout_service._check и geometry.polygon_problems.
    name: str = Field(max_length=200)
    polygon: list[Point] = Field(max_length=100, description="Точки [[x, y], ...] в долях 0..1")
    scenario_types: list[str] = Field(
        description="Назначение зоны — сценарии с requires_zone (danger_zone, blocked_exit…)"
    )
    enabled: bool = Field(True, description="Активность зоны = enabled всех её правил")


class LayoutFrameRule(InputModel):
    scenario_type: str = Field(description="Сценарий без зоны (requires_zone = false)")
    enabled: bool
    params: dict[str, Any] | None = Field(None, description="Параметры; null — не менять")


class LayoutIn(InputModel):
    zones: list[LayoutZone] = Field(description="Полный набор зон: отсутствующие удаляются")
    frame_rules: list[LayoutFrameRule] = Field(
        default_factory=list, description="Правила на весь кадр; не перечисленные не меняются"
    )


class LayoutOut(BaseModel):
    zones: list[ZoneOut]
    rules: list[RuleOut]
