"""Смены руководителей, рейтинг безопасности, экстренный вызов."""

from pydantic import BaseModel, Field

from app.schemas.common import ApiModel, InputModel, UtcDatetime


class ShiftCreate(InputModel):
    supervisor: str = Field(min_length=1, max_length=100, examples=["Петров И.С."])
    site: str | None = Field(None, max_length=200, description="Участок; пусто — все камеры")
    started_at: UtcDatetime | None = Field(None, description="Начало; по умолчанию — сейчас")
    ended_at: UtcDatetime | None = Field(None, description="Конец; null — смена идёт")


class ShiftUpdate(InputModel):
    supervisor: str | None = Field(None, min_length=1, max_length=100)
    site: str | None = Field(None, max_length=200)
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None


class ShiftOut(ApiModel):
    id: str
    supervisor: str
    site: str | None
    started_at: UtcDatetime
    ended_at: UtcDatetime | None
    created_at: UtcDatetime


class SupervisorRating(BaseModel):
    rank: int = Field(description="Место: 1 — меньше всего опасных ситуаций на смену")
    supervisor: str
    shifts: int = Field(description="Смен в периоде")
    hours: float = Field(description="Часов смен в периоде")
    incidents: int = Field(description="Опасных ситуаций: эпизоды смены, кроме ложных")
    confirmed: int = Field(description="Из них подтверждено оператором")
    unreviewed: int = Field(description="Из них ещё не проверено")
    false_positive: int = Field(description="Ложных срабатываний (в рейтинг не входят)")
    per_shift: float | None = Field(description="Опасных ситуаций на 8 часов смены")
    active_now: bool = Field(description="Смена руководителя идёт сейчас")


class EmergencyContact(BaseModel):
    number: str
    name: str


class EmergencyCall(InputModel):
    number: str = Field(min_length=1, max_length=20, description="Номер службы из списка")
    author: str = Field(min_length=1, max_length=100)
    comment: str | None = Field(None, max_length=2000, description="Что сообщили службе")
