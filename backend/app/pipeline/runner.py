"""Сборка конвейера из конфигурации источника и покадровый прогон.

FrameSource -> Detector -> Tracker -> ScenarioRule(s) -> EpisodeManager -> EpisodeSink
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np

import app.pipeline.scenarios  # noqa: F401 — регистрирует встроенные сценарии
from app.models.base import utcnow
from app.pipeline.episodes import EpisodeManager, EpisodeSink, RuleContext
from app.pipeline.interfaces import (
    BaseScenarioParams,
    Detector,
    Frame,
    FrameSource,
    RuleHit,
    ScenarioRule,
    Track,
    Tracker,
    ZoneGeometry,
)
from app.pipeline.scenarios.registry import get_scenario
from app.pipeline.tracker import TrackerConfig, build_tracker
from app.schemas.enums import Priority
from app.schemas.internal import SourceConfig

logger = logging.getLogger(__name__)


class PipelineStats:
    """FPS и время инференса по скользящему окну — для heartbeat и GET /metrics."""

    def __init__(self, window: int = 100) -> None:
        self.frames_processed = 0
        self._inference_ms: deque[float] = deque(maxlen=window)
        self._preprocess_ms: deque[float] = deque(maxlen=window)
        self._postprocess_ms: deque[float] = deque(maxlen=window)
        self._frame_times: deque[float] = deque(maxlen=window)

    def record(
        self,
        inference_ms: float,
        preprocess_ms: float | None = None,
        postprocess_ms: float | None = None,
    ) -> None:
        self.frames_processed += 1
        self._inference_ms.append(inference_ms)
        if preprocess_ms is not None:
            self._preprocess_ms.append(preprocess_ms)
        if postprocess_ms is not None:
            self._postprocess_ms.append(postprocess_ms)
        self._frame_times.append(time.monotonic())

    @property
    def preprocess_ms_avg(self) -> float | None:
        return round(float(np.mean(self._preprocess_ms)), 2) if self._preprocess_ms else None

    @property
    def postprocess_ms_avg(self) -> float | None:
        return round(float(np.mean(self._postprocess_ms)), 2) if self._postprocess_ms else None

    @property
    def fps(self) -> float | None:
        if len(self._frame_times) < 2:
            return None
        span = self._frame_times[-1] - self._frame_times[0]
        return round((len(self._frame_times) - 1) / span, 2) if span > 0 else None

    @property
    def inference_ms_avg(self) -> float | None:
        return round(float(np.mean(self._inference_ms)), 2) if self._inference_ms else None

    @property
    def inference_ms_p95(self) -> float | None:
        if not self._inference_ms:
            return None
        return round(float(np.percentile(self._inference_ms, 95)), 2)


@dataclass(frozen=True, slots=True)
class FrameResult:
    """Что конвейер увидел на кадре — для живого просмотра."""

    tracks: list[Track]  # все живые треки, включая tentative и lost
    hits: list[RuleHit]
    alerting_track_ids: set[int]  # треки с подтверждённым (активным) эпизодом
    # track_id -> сценарии с активным эпизодом / сведения сценариев (скорость и т.п.)
    alerting_scenarios: dict[int, set[str]] = field(default_factory=dict)
    track_info: dict[int, dict] = field(default_factory=dict)


@dataclass(slots=True)
class _ActiveRule:
    instance: ScenarioRule
    params: BaseScenarioParams
    zones: list[ZoneGeometry]


class Pipeline:
    def __init__(
        self,
        config: SourceConfig,
        detector: Detector,
        sink: EpisodeSink,
        *,
        tracker: Tracker | None = None,
        tracker_config: TrackerConfig | None = None,
        run_started_at: datetime | None = None,
    ) -> None:
        self.config = config
        self.detector = detector
        self.stats = PipelineStats()
        self.zones = {
            z.id: ZoneGeometry(id=z.id, name=z.name, polygon=[tuple(p) for p in z.polygon])
            for z in config.zones
        }

        self.rules: list[_ActiveRule] = []
        contexts: dict[str, RuleContext] = {}
        for rule in config.rules:
            try:
                cls = get_scenario(rule.scenario_type)
            except KeyError:
                logger.warning(
                    "Правило %s: сценарий %s не найден — пропускаем", rule.id, rule.scenario_type
                )
                continue
            params = cls.ParamsModel.model_validate(rule.params)
            if rule.zone_id:
                zones = [self.zones[rule.zone_id]]
            else:
                # Правило без зоны: сценарию, которому зона обязательна, — все зоны источника;
                # сценарию без обязательной зоны — весь кадр (пустой список зон).
                zones = list(self.zones.values()) if cls.requires_zone else []
            self.rules.append(_ActiveRule(cls(rule.id), params, zones))
            contexts[rule.id] = RuleContext(
                rule_id=rule.id,
                scenario_type=cls.scenario_type,
                title=cls.title,
                priority=Priority(rule.priority),
                version=rule.version,
                params=params,
                confirms_itself=cls.confirms_itself,
            )

        # Трек должен пережить допустимый пропуск наблюдения, иначе после перекрытия
        # человек получит новый track_id и новый эпизод.
        max_gap = max((r.params.gap_tolerance_s for r in self.rules), default=1.0)
        cfg = tracker_config or TrackerConfig()
        if cfg.buffer_s < max_gap:
            logger.warning(
                "TRACK_BUFFER_S=%.1f < gap_tolerance_s правил (%.1f) — буфер увеличен до %.1f, "
                "иначе после перекрытия получится новый трек и повторная тревога",
                cfg.buffer_s, max_gap, max_gap,
            )  # fmt: skip
            cfg = replace(cfg, buffer_s=max_gap)
        self.tracker = tracker or build_tracker(cfg)
        self.episodes = EpisodeManager(
            source_id=config.source.id,
            source_name=config.source.name,
            rules=contexts,
            zones=self.zones,
            sink=sink,
            model_version=detector.model_version,
            run_started_at=run_started_at or utcnow(),
            update_interval_s=config.settings.episode_update_interval_s,
        )

    def process_frame(self, frame: Frame) -> FrameResult:
        dets = self.detector.infer(frame)
        self.stats.record(dets.inference_ms, dets.preprocess_ms, dets.postprocess_ms)
        tracks = self.tracker.update(dets)
        confirmed = [t for t in tracks if t.state == "confirmed"]
        hits: list[RuleHit] = []
        info: dict[int, dict] = {}
        finished: list[tuple[str, str | None, int]] = []
        for rule in self.rules:
            hits.extend(rule.instance.update(confirmed, rule.zones, rule.params, frame.video_ts))
            for tid, data in rule.instance.track_info().items():
                info.setdefault(tid, {}).update(data)
            scenario = str(rule.instance.scenario_type)
            finished.extend((scenario, zone_id, tid) for tid, zone_id in rule.instance.finished())
        self.episodes.process(frame, hits, confirmed, finished)
        return FrameResult(
            tracks,
            hits,
            self.episodes.alerting_track_ids(),
            self.episodes.alerting_scenarios(),
            info,
        )

    def run(
        self,
        source: FrameSource,
        should_stop: Callable[[], bool] = lambda: False,
        on_frame: Callable[[Frame, FrameResult], None] | None = None,
    ) -> None:
        """Обрабатывает кадры до конца видео или should_stop(). Эпизоды завершаются в любом
        случае, в том числе при исключении детектора или источника (оно пробрасывается дальше)."""
        try:
            for frame in source:
                if should_stop():
                    break
                result = self.process_frame(frame)
                if on_frame is not None:
                    on_frame(frame, result)
        finally:
            source.close()
            try:
                self.episodes.finish()
            except Exception:
                # Не маскируем исходную ошибку; незакрытые эпизоды закроет backend.
                logger.exception("Не удалось отправить завершение эпизодов")
