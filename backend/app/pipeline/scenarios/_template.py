"""ШАБЛОН нового сценария. Скопируйте файл, например в scenarios/no_helmet.py, и заполните.

Модуль не импортируется, поэтому шаблон в реестр не попадает.

Как добавить сценарий (backend, API и фронт менять не нужно):
  1. Добавьте значение в ScenarioType (app/schemas/enums.py), например NO_HELMET = "no_helmet".
  2. Скопируйте этот файл, задайте Params и логику update().
  3. Импортируйте модуль в app/pipeline/scenarios/__init__.py — декоратор @register_scenario
     зарегистрирует класс.
  4. Готово: сценарий появится в GET /api/v1/scenarios с JSON Schema параметров (по ней фронт
     строит форму), правила создаются через POST /api/v1/rules, параметры проверяются автоматически.

Архитектурные правила (обязательны):
  - Правило — ЧИСТАЯ функция кадра (паттерн Strategy): update(context, zones, params) смотрит
    только на FrameContext и возвращает сигналы ЭТОГО кадра. Никакого состояния между кадрами:
    ни таймеров, ни «защёлок», ни решений о тревоге. Два вызова с одинаковым контекстом дают
    одинаковый результат.
  - Подтверждение (confirm_window_s), гистерезис, завершение (end_window_s, gap_tolerance_s),
    подавление повторов и тревоги — делает EpisodeManager одинаково для всех сценариев.
  - Уровни сигнала: VIOLATION — нарушение есть; HOLD — слабее порога начала, но продолжает идущий
    эпизод; UNKNOWN — оценить нельзя (с extra["reason"]); нет хита — нарушения нет.
  - Детектор, трекер и анализ сцены уже отработали один раз на кадр — читайте их результат из
    context (confirmed_tracks, all_tracks, detections, scene). Не вызывайте их и не читайте кадры.
  - Математика — в общих модулях: geometry.py, kinematics.py (скорость по истории трека),
    scene.py (маски, фон). Нужна новая функция — добавьте её туда, а не в сценарий.
  - Метаданные для события и доказательства — в RuleHit.extra: details (числа в event.details),
    label, trail, shapes (фигуры для annotate.py). Отрисовка общая, в сценарии рисовать нельзя.
  - Время — только video_ts.
"""

from collections.abc import Sequence
from typing import cast

from pydantic import Field

from app.pipeline.geometry import foot_point, point_in_polygon
from app.pipeline.interfaces import (
    BaseScenarioParams,
    FrameContext,
    HitLevel,
    RuleHit,
    ScenarioRule,
    ZoneGeometry,
)
from app.pipeline.kinematics import window_points
from app.pipeline.scenarios.registry import register_scenario  # noqa: F401
from app.schemas.enums import ScenarioType


class FallImmobilityParams(BaseScenarioParams):
    """Параметры сценария: Pydantic-модель, у КАЖДОГО поля должен быть дефолт и description
    (из неё фронт строит форму). min_confidence, confirm_window_s, gap_tolerance_s, end_window_s
    наследуются; дефолты можно переопределить."""

    confirm_window_s: float = Field(5.0, ge=0, le=120, description="Сколько секунд лежит")
    max_aspect_ratio: float = Field(
        0.8, gt=0, description="Рамка ниже/шире этого отношения высоты к ширине — «лежит»"
    )
    max_motion_h: float = Field(
        0.1, ge=0, description="Максимальное смещение ног за 1 с, в ростах — «не двигается»"
    )
    labels: list[str] = Field(default_factory=lambda: ["person"], description="Классы объектов")


# @register_scenario   <- раскомментируйте в своей копии
class FallImmobilityRule(ScenarioRule):
    scenario_type = ScenarioType.FALL_IMMOBILITY
    ParamsModel = FallImmobilityParams
    title = "Падение / неподвижность"
    description = "Человек лежит и не двигается дольше confirm_window_s"
    requires_zone = False  # True — правилу обязательно нужна зона
    auto_enable = False  # True — правило на весь кадр создаётся само для каждого источника

    def update(
        self, context: FrameContext, zones: Sequence[ZoneGeometry], params: BaseScenarioParams
    ) -> list[RuleHit]:
        """Сигналы ЭТОГО кадра (по одному на трек и зону). Только чтение context."""
        p = cast(FallImmobilityParams, params)
        hits: list[RuleHit] = []
        for track in context.confirmed_tracks:
            if track.label not in p.labels or track.confidence < p.min_confidence:
                continue
            x1, y1, x2, y2 = track.bbox
            pts = window_points(track, context.video_ts, 1.0)  # история трека — из трекера
            if len(pts) < 3:
                level = HitLevel.UNKNOWN  # оценить нельзя: мало наблюдений
            else:
                lying = (y2 - y1) / max(x2 - x1, 1e-6) < p.max_aspect_ratio
                move = max(abs(pts[-1].foot_x - pts[0].foot_x), abs(pts[-1].foot_y - pts[0].foot_y))
                still = move <= p.max_motion_h * max(pts[-1].bbox_h, 1.0)
                if not (lying and still):
                    continue  # нарушения нет
                level = HitLevel.VIOLATION
            # Если зоны заданы — нарушение только внутри них; без зон — весь кадр.
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
                        video_ts=context.video_ts,
                        label=track.label,
                        level=level,
                        extra={"label": "ЛЕЖИТ", "reason": "мало наблюдений"}
                        if level == HitLevel.UNKNOWN
                        else {"label": "ЛЕЖИТ"},
                    )
                )
        return hits
