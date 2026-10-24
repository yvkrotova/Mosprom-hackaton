"""Сценарий «бег / резкое перемещение» — чистая функция кадра.

Скорость — kinematics.estimate_speed по истории трека (роста человека в секунду, не м/с).
На каждом кадре по каждому треку:
    скорость ≥ speed_threshold              → VIOLATION (открывает и продолжает эпизод);
    скорость ≥ speed_threshold × end_ratio  → HOLD (только продолжает идущий эпизод — гистерезис);
    скорость неизвестна                     → UNKNOWN (молодой трек, рамка у края, скачет высота);
    иначе                                   → нет хита (нарушения нет).
Подтверждение (confirm_window_s), завершение (end_window_s, gap_tolerance_s) и тревоги делает
EpisodeManager одинаково для всех сценариев. С зоной — только когда ноги внутри полигона.

Дефолты — по калибровке на тестовых роликах (docs/BENCHMARKS.md): порог 1.7 — между быстрым
подходом к месту аварии (до 1.59, ролик coils) и бегом (медианы 1.8–2.2, KTH); ходьба ≤ 0.72.
confirm_window_s 0.6 и min_track_age_s 0.3 — бегущий в KTH виден ~1 с; все 4 пробежки находятся
при порогах 1.5–1.8 и окнах 0.5–0.6. Подтверждены частично: бег есть только в одном ролике.
"""

from collections.abc import Sequence
from typing import Any, cast

from pydantic import Field

from app.pipeline.geometry import foot_point, point_in_polygon
from app.pipeline.interfaces import (
    BaseScenarioParams,
    FrameContext,
    HitLevel,
    RuleHit,
    ScenarioRule,
    Track,
    ZoneGeometry,
)
from app.pipeline.kinematics import SpeedEstimate, estimate_speed
from app.pipeline.scenarios.registry import register_scenario
from app.schemas.enums import ScenarioType

LABEL = "БЕГ / РЕЗКОЕ ПЕРЕМЕЩЕНИЕ"


class RunFastParams(BaseScenarioParams):
    labels: list[str] = Field(
        default_factory=lambda: ["person"],
        min_length=1,
        description="Классы объектов, у которых проверяется скорость",
    )
    confirm_window_s: float = Field(
        0.6,
        ge=0,
        le=10,
        description="Сколько секунд видео скорость должна держаться выше порога до тревоги",
    )
    end_window_s: float | None = Field(
        1.0, ge=0, le=10, description="Скорость ниже порога продолжения дольше этого — конец, с"
    )
    speed_threshold: float = Field(
        1.7,
        ge=0.2,
        le=10,
        description="Порог, «ростов человека в секунду» (не м/с): ходьба ≈ 0.6–1.0, бег ≈ 2–3",
    )
    end_ratio: float = Field(
        0.7,
        gt=0,
        le=1,
        description="Эпизод продолжается, пока скорость ≥ порог × end_ratio (гистерезис)",
    )
    speed_window_s: float = Field(
        0.6, ge=0.2, le=3, description="Окно оценки скорости (регрессия по точкам ног), с"
    )
    min_track_age_s: float = Field(
        0.3, ge=0, le=10, description="Трек моложе этого — скорость неизвестна, с"
    )
    min_points: int = Field(4, ge=2, le=100, description="Минимум наблюдений в окне скорости")
    max_height_change: float = Field(
        0.35,
        gt=0,
        le=2,
        description="Высота рамки в окне меняется больше этой доли — скорость неизвестна",
    )


def track_speed(track: Track, ts: float, p: RunFastParams) -> SpeedEstimate:
    return estimate_speed(
        track,
        ts,
        window_s=p.speed_window_s,
        min_track_age_s=p.min_track_age_s,
        min_points=p.min_points,
        max_height_change=p.max_height_change,
    )


@register_scenario
class RunFastMovementRule(ScenarioRule):
    scenario_type = ScenarioType.RUN_FAST_MOVEMENT
    ParamsModel = RunFastParams
    title = "Бег / резкое перемещение"
    icon = "run"
    details_fields = {"speed": ("Скорость", "роста/с")}
    description = (
        "Тревога, если человек перемещается быстрее порога в «ростах человека в секунду» "
        "(не м/с, камера неподвижна). С зоной — только внутри зоны, без зоны — весь кадр"
    )
    requires_zone = False
    auto_enable = True  # проверяется на любом видео без настройки

    def update(
        self, context: FrameContext, zones: Sequence[ZoneGeometry], params: BaseScenarioParams
    ) -> list[RuleHit]:
        p = cast(RunFastParams, params)
        ts = context.video_ts
        hits: list[RuleHit] = []
        for track in context.confirmed_tracks:
            if track.label not in p.labels or track.confidence < p.min_confidence:
                continue
            zone_id = None
            if zones:
                feet = foot_point(track.bbox)
                zone = next((z for z in zones if point_in_polygon(feet, z.polygon)), None)
                if zone is None:
                    continue  # вне зон правила — нарушения нет
                zone_id = zone.id
            est = track_speed(track, ts, p)
            if est.speed is None:
                level = HitLevel.UNKNOWN
            elif est.speed >= p.speed_threshold:
                level = HitLevel.VIOLATION
            elif est.speed >= p.speed_threshold * p.end_ratio:
                level = HitLevel.HOLD
            else:
                continue
            extra: dict[str, Any] = {"reason": est.reason} if est.speed is None else {
                "details": {"speed": round(est.speed, 2)},
                "label": f"{LABEL} · {est.speed:.1f} роста/с",
                "trail": True,
            }  # fmt: skip
            hits.append(
                RuleHit(
                    scenario_type=self.scenario_type,
                    rule_id=self.rule_id,
                    track_id=track.track_id,
                    zone_id=zone_id,
                    confidence=track.confidence,
                    bbox=track.bbox,
                    video_ts=ts,
                    label=track.label,
                    level=level,
                    extra=extra,
                )
            )
        return hits

    def describe(
        self, context: FrameContext, zones: Sequence[ZoneGeometry], params: BaseScenarioParams
    ) -> dict[int, dict[str, Any]]:
        p = cast(RunFastParams, params)
        out = {}
        for track in context.confirmed_tracks:
            if track.label in p.labels:
                est = track_speed(track, context.video_ts, p)
                out[track.track_id] = {
                    "speed": round(est.speed, 2) if est.speed is not None else None,
                    "speed_unknown": est.reason,
                }
        return out
