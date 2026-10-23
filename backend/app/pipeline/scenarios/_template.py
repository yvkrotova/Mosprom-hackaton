"""ШАБЛОН нового сценария. Скопируйте файл, например в scenarios/no_helmet.py, и заполните.

Модуль не импортируется, поэтому шаблон в реестр не попадает.

Как добавить сценарий (backend, API и фронт менять не нужно):
  1. Добавьте значение в ScenarioType (app/schemas/enums.py), например NO_HELMET = "no_helmet".
  2. Скопируйте этот файл, задайте Params и логику update().
  3. Импортируйте модуль в app/pipeline/scenarios/__init__.py — декоратор @register_scenario
     зарегистрирует класс.
  4. Готово: сценарий появится в GET /api/v1/scenarios с JSON Schema параметров, правила
     для него создаются через POST /api/v1/rules, параметры проверяются автоматически.

Чем НЕ надо заниматься в сценарии:
  - подтверждением по времени, допуском потери, подавлением повторов, тревогами —
    это делает EpisodeManager одинаково для всех сценариев по параметрам
    confirm_window_s и gap_tolerance_s (они уже есть в BaseScenarioParams);
  - временем на часах: используйте только video_ts и track.first_seen_ts / last_seen_ts.
"""

from collections.abc import Sequence
from typing import cast

from pydantic import Field

from app.pipeline.geometry import foot_point, point_in_polygon
from app.pipeline.interfaces import (
    BaseScenarioParams,
    RuleHit,
    ScenarioRule,
    Track,
    ZoneGeometry,
)
from app.pipeline.scenarios.registry import register_scenario  # noqa: F401
from app.schemas.enums import ScenarioType


class FallImmobilityParams(BaseScenarioParams):
    """Параметры сценария: Pydantic-модель, у КАЖДОГО поля должен быть дефолт.

    min_confidence, confirm_window_s и gap_tolerance_s наследуются; дефолты можно переопределить.
    Из этой модели строится JSON Schema для фронта и проверяются params правила (422 при ошибке).
    """

    confirm_window_s: float = Field(5.0, ge=0, le=120, description="Сколько секунд лежит")
    max_aspect_ratio: float = Field(
        0.8, gt=0, description="Рамка ниже/шире этого отношения высоты к ширине — «лежит»"
    )
    max_motion: float = Field(
        0.02, ge=0, description="Максимальное смещение центра рамки за кадр (доли кадра)"
    )
    labels: list[str] = Field(default_factory=lambda: ["person"])


# @register_scenario   <- раскомментируйте в своей копии
class FallImmobilityRule(ScenarioRule):
    scenario_type = ScenarioType.FALL_IMMOBILITY
    ParamsModel = FallImmobilityParams
    title = "Падение / неподвижность"
    description = "Человек лежит и не двигается дольше confirm_window_s"
    requires_zone = False  # True — правилу обязательно нужна зона

    def __init__(self, rule_id: str) -> None:
        super().__init__(rule_id)
        # Состояние между кадрами храните здесь (один экземпляр = одно правило одного источника).
        self._last_center: dict[int, tuple[float, float]] = {}

    def update(
        self,
        tracks: Sequence[Track],
        zones: Sequence[ZoneGeometry],
        params: BaseScenarioParams,
        video_ts: float,
    ) -> list[RuleHit]:
        """Вызывается на каждом кадре. Вернуть нарушения ЭТОГО кадра (по одному на трек и зону).

        tracks — треки, видимые на кадре; zones — зона правила или все зоны источника;
        если сценарий не про зоны, верните zone_id=None.
        """
        params = cast(FallImmobilityParams, params)
        hits: list[RuleHit] = []
        for track in tracks:
            if track.label not in params.labels or track.confidence < params.min_confidence:
                continue
            x1, y1, x2, y2 = track.bbox
            lying = (y2 - y1) / max(x2 - x1, 1e-6) < params.max_aspect_ratio
            center = ((x1 + x2) / 2, (y1 + y2) / 2)
            prev = self._last_center.get(track.track_id, center)
            still = abs(center[0] - prev[0]) + abs(center[1] - prev[1]) <= params.max_motion
            self._last_center[track.track_id] = center
            if not (lying and still):
                continue
            # Пример привязки к зоне: если зоны заданы — нарушение только внутри них.
            zone_ids: list[str | None] = [
                z.id for z in zones if point_in_polygon(foot_point(track.bbox), z.polygon)
            ] or ([None] if not zones else [])
            for zone_id in zone_ids:
                hits.append(
                    RuleHit(
                        scenario_type=self.scenario_type,
                        rule_id=self.rule_id,
                        track_id=track.track_id,
                        zone_id=zone_id,
                        confidence=track.confidence,
                        bbox=track.bbox,
                        video_ts=video_ts,
                        label=track.label,
                    )
                )
        return hits
