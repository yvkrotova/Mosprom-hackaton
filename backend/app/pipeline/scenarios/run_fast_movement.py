"""Сценарий «бег / резкое перемещение».

Скорость не зависит от расстояния до камеры: смещение точки ног (пиксели исходного кадра) делится
на медианную высоту рамки за окно и на Δvideo_ts. Единица — «ростов человека в секунду»
(ориентир: ходьба ≈ 0.6–1.0, бег ≈ 2–3). Это НЕ м/с; камера должна быть неподвижной.

Порог по умолчанию 1.5 — по калибровке на тестовых роликах (docs/BENCHMARKS.md): бег 1.8–2.2,
ходьба ≤ 0.6. Подтверждён частично: бег есть только в одном ролике.

По каждому треку:
1. скорость — наклон линейной регрессии положения ног по точкам окна speed_window_s, затем EMA;
2. скорость «неизвестна» (а не ноль), если трек слишком молодой, точек мало, рамка касается края
   кадра или высота рамки в окне скачет больше max_height_change (перекрытие, ошибка детектора);
3. гистерезис: кандидат при speed ≥ speed_threshold, подтверждение через confirm_window_s;
   эпизод держится, пока speed ≥ speed_threshold·end_ratio, и завершается, если скорость ниже
   дольше end_window_s (или трек потерян дольше gap_tolerance_s — это делает EpisodeManager);
4. с зоной — только когда ноги внутри полигона; правило без зоны — весь кадр.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any, cast

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

EDGE = 0.005  # рамка ближе к краю кадра — человек обрезан, точка ног недостоверна
LABEL = "БЕГ / РЕЗКОЕ ПЕРЕМЕЩЕНИЕ"


class RunFastParams(BaseScenarioParams):
    labels: list[str] = Field(
        default_factory=lambda: ["person"], min_length=1,
        description="Классы объектов, у которых проверяется скорость",
    )  # fmt: skip
    confirm_window_s: float = Field(
        0.8,
        ge=0,
        le=10,
        description="Сколько секунд видео скорость должна держаться выше порога до тревоги",
    )
    speed_threshold: float = Field(
        1.5, ge=0.2, le=10,
        description="Порог, «ростов человека в секунду» (не м/с): ходьба ≈ 0.6–1.0, бег ≈ 2–3",
    )  # fmt: skip
    end_ratio: float = Field(
        0.7,
        gt=0,
        le=1,
        description="Эпизод продолжается, пока скорость ≥ порог × end_ratio (гистерезис)",
    )
    speed_window_s: float = Field(
        0.6, ge=0.2, le=3, description="Окно оценки скорости (регрессия по точкам ног), с"
    )
    ema_alpha: float = Field(
        0.4, gt=0, le=1, description="Сглаживание скорости: доля нового значения"
    )
    end_window_s: float = Field(
        1.0, ge=0, le=10, description="Скорость ниже порога дольше этого — эпизод завершается, с"
    )
    min_track_age_s: float = Field(
        0.5, ge=0, le=10, description="Трек моложе этого — скорость неизвестна, с"
    )
    min_points: int = Field(4, ge=2, le=100, description="Минимум наблюдений в окне скорости")
    max_height_change: float = Field(
        0.35, gt=0, le=2,
        description="Высота рамки в окне меняется больше этой доли — скорость неизвестна",
    )  # fmt: skip


@dataclass(slots=True)
class _TrackState:
    ema: float | None = None
    raw: float | None = None
    unknown: str | None = None
    active_since: float | None = None  # начало непрерывного превышения (кандидат)
    last_active_ts: float = 0.0
    last_known_ts: float = 0.0  # последний кадр с известной скоростью
    below_since: float | None = None  # с какого момента скорость известна и ниже порога
    latched: bool = False  # нарушение подтверждено (идёт эпизод)
    zone_id: str | None = None
    last_seen_ts: float = 0.0


def estimate_speed(
    track: Track, params: RunFastParams, ts: float
) -> tuple[float | None, str | None]:
    """(скорость в ростах/с, None) или (None, причина «неизвестна»)."""
    if track.state != "confirmed":
        return None, "трек не подтверждён"
    if ts - track.first_seen_ts < params.min_track_age_s:
        return None, "трек слишком молодой"
    x1, y1, x2, y2 = track.bbox
    if x1 <= EDGE or y1 <= EDGE or x2 >= 1 - EDGE or y2 >= 1 - EDGE:
        return None, "рамка у края кадра"
    pts = [p for p in track.history if ts - p.video_ts <= params.speed_window_s + 1e-6]
    if len(pts) < params.min_points:
        return None, "мало точек в окне"
    heights = [p.bbox_h for p in pts]
    med_h = median(heights)
    if med_h <= 0 or (max(heights) - min(heights)) / med_h > params.max_height_change:
        return None, "высота рамки скачет"
    t0 = sum(p.video_ts for p in pts) / len(pts)
    x0 = sum(p.foot_x for p in pts) / len(pts)
    y0 = sum(p.foot_y for p in pts) / len(pts)
    denom = sum((p.video_ts - t0) ** 2 for p in pts)
    if denom <= 0:
        return None, "мало точек в окне"
    vx = sum((p.video_ts - t0) * (p.foot_x - x0) for p in pts) / denom
    vy = sum((p.video_ts - t0) * (p.foot_y - y0) for p in pts) / denom
    return (vx * vx + vy * vy) ** 0.5 / med_h, None


@register_scenario
class RunFastMovementRule(ScenarioRule):
    scenario_type = ScenarioType.RUN_FAST_MOVEMENT
    ParamsModel = RunFastParams
    title = "Бег / резкое перемещение"
    description = (
        "Тревога, если человек перемещается быстрее порога в «ростах человека в секунду» "
        "(не м/с, камера неподвижна). С зоной — только внутри зоны, без зоны — весь кадр"
    )
    requires_zone = False
    confirms_itself = True

    def __init__(self, rule_id: str) -> None:
        super().__init__(rule_id)
        self._states: dict[int, _TrackState] = {}
        self._info: dict[int, dict[str, Any]] = {}
        self._finished: list[tuple[int, str | None]] = []

    def update(
        self,
        tracks: Sequence[Track],
        zones: Sequence[ZoneGeometry],
        params: BaseScenarioParams,
        video_ts: float,
    ) -> list[RuleHit]:
        p = cast(RunFastParams, params)
        ts = video_ts
        hits: list[RuleHit] = []
        self._info = {}
        self._finished = []
        for track in tracks:
            if track.label not in p.labels or track.confidence < p.min_confidence:
                continue
            st = self._states.setdefault(track.track_id, _TrackState())
            st.last_seen_ts = ts
            raw, st.unknown = estimate_speed(track, p, ts)
            st.raw = raw
            if raw is not None:
                st.ema = raw if st.ema is None else p.ema_alpha * raw + (1 - p.ema_alpha) * st.ema

            zone_id, in_area = None, True
            if zones:
                feet = foot_point(track.bbox)
                zone = next((z for z in zones if point_in_polygon(feet, z.polygon)), None)
                zone_id, in_area = (zone.id if zone else None), zone is not None

            known = raw is not None
            if known:
                st.last_known_ts = ts
            fast = self._is_fast(st, p, known)
            if fast and in_area:
                st.last_active_ts, st.below_since = ts, None
                if st.active_since is None:
                    st.active_since = ts
            elif st.latched and in_area and self._holds(st, p, known, ts):
                pass  # медленнее порога или скорость неизвестна, но недолго — эпизод продолжается
            else:
                if st.latched:
                    self._finished.append((track.track_id, st.zone_id))
                st.latched, st.active_since, st.below_since = False, None, None

            if (
                st.active_since is not None
                and not st.latched
                and ts - st.active_since >= p.confirm_window_s
            ):
                st.latched, st.zone_id = True, zone_id
            if st.latched:
                hits.append(
                    RuleHit(
                        scenario_type=self.scenario_type,
                        rule_id=self.rule_id,
                        track_id=track.track_id,
                        zone_id=st.zone_id,
                        confidence=track.confidence,
                        bbox=track.bbox,
                        video_ts=ts,
                        label=track.label,
                        extra={
                            "details": {"speed": round(st.ema or 0.0, 2)},
                            "start_ts": round(st.active_since or ts, 3),
                            "label": f"{LABEL} · {st.ema or 0:.1f} роста/с",
                            "trail": True,
                        },
                    )
                )
            self._info[track.track_id] = {
                "speed": round(st.ema, 2) if st.ema is not None and raw is not None else None,
                "candidate": st.active_since is not None and not st.latched,
                "speed_unknown": st.unknown,
            }

        # Ушедшие треки забываем (их эпизоды закроет EpisodeManager по gap_tolerance_s).
        for tid in [t for t, s in self._states.items() if ts - s.last_seen_ts > p.gap_tolerance_s]:
            del self._states[tid]
        return hits

    @staticmethod
    def _holds(st: _TrackState, p: RunFastParams, known: bool, ts: float) -> bool:
        """Идущий эпизод держится: скорость известна и ниже порога не дольше end_window_s,
        или скорость неизвестна (перекрытие, край кадра) не дольше gap_tolerance_s."""
        if known:
            if st.below_since is None:
                st.below_since = ts
            return ts - st.below_since <= p.end_window_s
        return ts - st.last_known_ts <= p.gap_tolerance_s

    @staticmethod
    def _is_fast(st: _TrackState, p: RunFastParams, known: bool) -> bool:
        if not known or st.ema is None:
            return False
        threshold = p.speed_threshold * (p.end_ratio if st.latched else 1.0)
        return st.ema >= threshold

    def track_info(self) -> dict[int, dict[str, Any]]:
        return self._info

    def finished(self) -> list[tuple[int, str | None]]:
        return self._finished
