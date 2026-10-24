"""Сценарии нарушений. Импорт модуля регистрирует сценарий в реестре."""

from app.pipeline.scenarios import (  # noqa: F401
    blocked_exit,
    danger_zone,
    run_fast_movement,
)
from app.pipeline.scenarios.registry import get_scenario, list_scenarios, register_scenario

__all__ = ["get_scenario", "list_scenarios", "register_scenario"]
