"""Сценарий «человек в опасной зоне»."""

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
from app.pipeline.scenarios.registry import register_scenario
from app.schemas.enums import ScenarioType


class DangerZoneParams(BaseScenarioParams):
    labels: list[str] = Field(
        default_factory=lambda: ["person"],
        min_length=1,
        description="Классы объектов, которые считаются нарушителями",
    )


@register_scenario
class DangerZoneRule(ScenarioRule):
    scenario_type = ScenarioType.DANGER_ZONE
    ParamsModel = DangerZoneParams
    title = "Человек в опасной зоне"
    description = "Тревога, если ноги человека (центр нижней кромки рамки) внутри полигона зоны"
    requires_zone = True

    def update(
        self,
        tracks: Sequence[Track],
        zones: Sequence[ZoneGeometry],
        params: BaseScenarioParams,
        video_ts: float,
    ) -> list[RuleHit]:
        params = cast(DangerZoneParams, params)
        hits: list[RuleHit] = []
        for track in tracks:
            if track.label not in params.labels or track.confidence < params.min_confidence:
                continue
            feet = foot_point(track.bbox)
            for zone in zones:
                if point_in_polygon(feet, zone.polygon):
                    hits.append(
                        RuleHit(
                            scenario_type=self.scenario_type,
                            rule_id=self.rule_id,
                            track_id=track.track_id,
                            zone_id=zone.id,
                            confidence=track.confidence,
                            bbox=track.bbox,
                            video_ts=video_ts,
                            label=track.label,
                        )
                    )
        return hits
