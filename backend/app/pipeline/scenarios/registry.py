"""Реестр сценариев: новый сценарий = новый модуль с классом под @register_scenario.

Чтобы сценарий попал в реестр, его модуль импортируется в scenarios/__init__.py.
"""

from app.pipeline.interfaces import ScenarioRule

_REGISTRY: dict[str, type[ScenarioRule]] = {}


def register_scenario(cls: type[ScenarioRule]) -> type[ScenarioRule]:
    key = str(cls.scenario_type)
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        raise ValueError(f"Сценарий {key} уже зарегистрирован классом {_REGISTRY[key].__name__}")
    _REGISTRY[key] = cls
    return cls


def get_scenario(scenario_type: str) -> type[ScenarioRule]:
    """KeyError, если сценарий не зарегистрирован."""
    return _REGISTRY[str(scenario_type)]


def list_scenarios() -> list[type[ScenarioRule]]:
    return sorted(_REGISTRY.values(), key=lambda cls: str(cls.scenario_type))
