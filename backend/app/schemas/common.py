"""Общие типы контракта: UUID-строки, время в UTC, относительные координаты, формат ошибок."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.pipeline.geometry import polygon_problems


def _check_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("ожидается UUID-строка") from exc


def _to_utc(value: datetime) -> datetime:
    # Время без часового пояса (в т.ч. из SQLite) считаем UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UuidStr = Annotated[str, AfterValidator(_check_uuid)]
# Сериализуется как ISO 8601 с суффиксом Z: "2026-10-23T09:15:02.120000Z".
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]

RelCoord = Annotated[float, Field(ge=0.0, le=1.0)]
Point = tuple[RelCoord, RelCoord]


def _check_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        raise ValueError("bbox должен быть [x1, y1, x2, y2] с x2 > x1 и y2 > y1")
    return bbox


def _check_polygon(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    # Общая проверка контура (та же — во фронте): 3 разные точки, площадь, самопересечения.
    problems = polygon_problems(points)
    if problems:
        raise ValueError("; ".join(problems))
    return points


# Относительные координаты 0..1 от размеров кадра.
BBox = Annotated[tuple[RelCoord, RelCoord, RelCoord, RelCoord], AfterValidator(_check_bbox)]
Polygon = Annotated[
    list[Point], Field(min_length=3, max_length=100), AfterValidator(_check_polygon)
]


class ApiModel(BaseModel):
    """База для схем ответа: умеет читать атрибуты ORM-объектов."""

    model_config = ConfigDict(from_attributes=True)


class InputModel(BaseModel):
    """База для схем запроса: лишние поля — ошибка (ловит опечатки на фронте)."""

    model_config = ConfigDict(extra="forbid")


class ErrorBody(BaseModel):
    code: str = Field(examples=["EVENT_NOT_FOUND"])
    message: str = Field(examples=["Событие не найдено"])
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Единый формат всех ошибок API."""

    error: ErrorBody


T = TypeVar("T")


class Page(BaseModel, Generic[T]):  # noqa: UP046 — PEP 695 недоступен в Python 3.11
    items: list[T]
    total: int
    limit: int
    offset: int
