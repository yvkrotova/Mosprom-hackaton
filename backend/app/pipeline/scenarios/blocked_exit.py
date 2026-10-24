"""Сценарий «заблокированный проход / эвакуационный выход» — чистая функция кадра.

Нейросеть знает не все предметы, поэтому препятствие ищет анализ сцены (scene.py): что отличается
от эталона пустого прохода и при этом «осело» (неподвижно); люди исключены по детекциям YOLO.
Правило по каждой зоне:
    сцена не готова / «оценить нельзя»                 → UNKNOWN (с причиной);
    площадь неподвижных пятен ≥ min_obstacle_area_pct   → VIOLATION (контуры, площадь, подпись);
    площадь ≥ hold_ratio × min_obstacle_area_pct        → HOLD (продолжает идущий эпизод);
    иначе                                               → нет хита.
Нарушение уровня зоны: track_id = None. Подтверждение (confirm_window_s — предмет лежит, а не
пронесли), завершение (end_window_s — убрали), длительность блокировки — в EpisodeManager.
"""

from collections.abc import Sequence
from typing import cast

from pydantic import Field

from app.pipeline.interfaces import (
    BaseScenarioParams,
    FrameContext,
    HitLevel,
    RuleHit,
    ScenarioRule,
    ZoneGeometry,
)
from app.pipeline.scenarios.registry import register_scenario
from app.pipeline.scene import SceneParams
from app.schemas.enums import ScenarioType

LABEL = "БЛОКИРОВКА ПРОХОДА"


class BlockedExitParams(BaseScenarioParams):
    confirm_window_s: float = Field(
        10.0, ge=0, le=600, description="Сколько секунд предмет должен лежать до тревоги"
    )
    end_window_s: float | None = Field(
        3.0, ge=0, le=600, description="Проход свободен дольше этого — эпизод завершается, с"
    )
    gap_tolerance_s: float = Field(
        5.0, ge=0, le=600, description="Сцену нельзя оценить дольше этого — эпизод завершается, с"
    )
    min_obstacle_area_pct: float = Field(
        0.05, gt=0, le=1, description="Предметы занимают не меньше этой доли площади зоны — тревога"
    )
    min_blob_area_pct: float = Field(
        0.01, ge=0, le=1, description="Пятна меньше этой доли площади зоны не считаются предметами"
    )
    hold_ratio: float = Field(
        0.6, gt=0, le=1, description="Эпизод продолжается, пока площадь ≥ порог × hold_ratio"
    )
    max_change_pct: float = Field(
        0.6,
        gt=0,
        le=1,
        description="Изменилось больше этой доли зоны — «оценить нельзя» (свет, сдвиг камеры)",
    )
    person_mask_margin: float = Field(
        0.15,
        ge=0,
        le=1,
        description="Запас вокруг рамки человека (доли её высоты): люди — не предмет",
    )
    diff_threshold: float = Field(
        28.0, ge=5, le=120, description="Порог отличия от пустого прохода, уровни яркости 0–255"
    )

    def scene_params(self) -> SceneParams:
        return SceneParams(
            diff_threshold=self.diff_threshold,
            person_mask_margin=self.person_mask_margin,
            max_change_pct=self.max_change_pct,
        )


@register_scenario
class BlockedExitRule(ScenarioRule):
    scenario_type = ScenarioType.BLOCKED_EXIT
    ParamsModel = BlockedExitParams
    title = "Заблокированный проход"
    description = (
        "Тревога, если в проходе (зоне) появился и лежит посторонний неподвижный предмет. "
        "Сравнение с пустым проходом: сохраните эталон, иначе он собирается "
        "по первым секундам видео"
    )
    requires_zone = True
    auto_enable = False
    requires_scene_analysis = True

    def update(
        self, context: FrameContext, zones: Sequence[ZoneGeometry], params: BaseScenarioParams
    ) -> list[RuleHit]:
        p = cast(BlockedExitParams, params)
        hits: list[RuleHit] = []
        for zone in zones:
            scene = (context.scene or {}).get(zone.id)
            if scene is None or not scene.ready:
                reason = scene.unknown_reason if scene else "анализ сцены не запущен"
                hits.append(self._hit(context, zone, HitLevel.UNKNOWN, {"reason": reason}))
                continue
            blobs = [b for b in scene.static_blobs if b["area_pct"] >= p.min_blob_area_pct]
            area = sum(b["area_pct"] for b in blobs)
            if area >= p.min_obstacle_area_pct:
                level = HitLevel.VIOLATION
            elif area >= p.hold_ratio * p.min_obstacle_area_pct:
                level = HitLevel.HOLD
            else:
                continue
            label = f"{LABEL} · {area * 100:.0f}% зоны"
            hits.append(
                self._hit(
                    context,
                    zone,
                    level,
                    {
                        "details": {"blocked_pct": round(area * 100, 1), "obstacles": len(blobs)},
                        "label": label,
                        "shapes": [
                            {
                                "polygon": b["polygon"],
                                "style": "violation",
                                "label": label if i == 0 else None,
                            }
                            for i, b in enumerate(blobs)
                        ],
                    },
                )
            )
        return hits

    def _hit(self, context: FrameContext, zone: ZoneGeometry, level: HitLevel, extra) -> RuleHit:
        return RuleHit(
            scenario_type=self.scenario_type,
            rule_id=self.rule_id,
            track_id=None,  # нарушение зоны, а не человека
            zone_id=zone.id,
            confidence=1.0,
            bbox=None,
            video_ts=context.video_ts,
            label="obstacle",
            level=level,
            extra=extra,
        )
