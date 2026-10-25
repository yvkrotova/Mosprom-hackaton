"""Все модели импортируются здесь, чтобы Base.metadata (и Alembic) видел каждую таблицу."""

from app.models.event import Event, Evidence, OperatorAction
from app.models.shift import Shift
from app.models.source import Rule, Source, Zone
from app.models.stats import ProcessingStat

__all__ = [
    "Event",
    "Evidence",
    "OperatorAction",
    "ProcessingStat",
    "Rule",
    "Shift",
    "Source",
    "Zone",
]
