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
    FrameContext,
    FrameSource,
    RuleHit,
    ScenarioRule,
    Track,
    Tracker,
    ZoneGeometry,
)
from app.pipeline.scenarios.registry import get_scenario
from app.pipeline.scene import SceneAnalyzer, SceneParams
from app.pipeline.tracker import TrackerConfig, build_tracker
from app.schemas.enums import Priority
from app.schemas.internal import SourceConfig
from app.schemas.sources import ZoneOut

logger = logging.getLogger(__name__)


class PipelineStats:
    """FPS и время инференса по скользящему окну — для heartbeat и GET /metrics."""

    def __init__(self, window: int = 100) -> None:
        self.frames_processed = 0
        self._inference_ms: deque[float] = deque(maxlen=window)
        self._preprocess_ms: deque[float] = deque(maxlen=window)
        self._postprocess_ms: deque[float] = deque(maxlen=window)
        self._scene_ms: deque[float] = deque(maxlen=window)
        self._frame_times: deque[float] = deque(maxlen=window)
        self._frame_ms: deque[float] = deque(maxlen=window)  # время обработки кадра этим процессом
        # Replay: записанное время инференса (историческое, на LinQ) — за весь прогон, отдельно
        # от скорости воспроизведения; в inference_ms_* оно не попадает.
        self.recorded_timings = False
        self._recorded_inference_ms: list[float] = []
        self.frames_unanalyzed = 0

    def record(
        self,
        inference_ms: float,
        preprocess_ms: float | None = None,
        postprocess_ms: float | None = None,
    ) -> None:
        self.frames_processed += 1
        if self.recorded_timings:
            self._recorded_inference_ms.append(inference_ms)
        else:
            self._inference_ms.append(inference_ms)
        if preprocess_ms is not None:
            self._preprocess_ms.append(preprocess_ms)
        if postprocess_ms is not None:
            self._postprocess_ms.append(postprocess_ms)
        self._frame_times.append(time.monotonic())

    def record_unanalyzed(self) -> None:
        """Кадр прочитан, но не проанализирован (нет записи детекций)."""
        self.frames_processed += 1
        self.frames_unanalyzed += 1
        self._frame_times.append(time.monotonic())

    def record_frame_ms(self, frame_ms: float) -> None:
        self._frame_ms.append(frame_ms)

    @property
    def frame_ms_avg(self) -> float | None:
        """Полное время обработки кадра этим процессом (детектор или чтение записи + трекер +
        правила), мс. Для replay это скорость воспроизведения, а не скорость LinQ."""
        return round(float(np.mean(self._frame_ms)), 2) if self._frame_ms else None

    def recorded_inference(self) -> dict[str, float | None]:
        values = self._recorded_inference_ms
        if not values:
            return {"avg": None, "p50": None, "p95": None}
        return {
            "avg": round(float(np.mean(values)), 2),
            "p50": round(float(np.percentile(values, 50)), 2),
            "p95": round(float(np.percentile(values, 95)), 2),
        }

    def record_scene(self, scene_ms: float) -> None:
        self._scene_ms.append(scene_ms)

    @property
    def scene_ms_avg(self) -> float | None:
        """Среднее время обновления анализа сцены (только кадры, где он считался)."""
        return round(float(np.mean(self._scene_ms)), 2) if self._scene_ms else None

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
    candidate_track_ids: set[int] = field(default_factory=set)  # кандидат открыт, не подтверждён
    zone_states: list[dict] = field(default_factory=list)  # EpisodeManager.zone_states()


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
        scene_hz: float = 4.0,
        scene_bootstrap_s: float = 3.0,
        reference_loader: Callable[[ZoneOut], np.ndarray | None] | None = None,
        event_id_seed: str | None = None,
    ) -> None:
        self.config = config
        self.detector = detector
        self.stats = PipelineStats()
        self.stats.recorded_timings = getattr(detector, "recorded_timings", False)
        self.zones, self.rules, contexts = self._build_rules(config)
        self._pending_config: SourceConfig | None = None  # из потока ConfigWatcher
        self.scene_hz, self.scene_bootstrap_s = scene_hz, scene_bootstrap_s
        self.reference_loader = reference_loader
        self.scene: SceneAnalyzer | None = None
        self._scene_key = None
        self._build_scene()

        # Трек должен пережить допустимый пропуск наблюдения, иначе после перекрытия
        # человек получит новый track_id и новый эпизод.
        # Правила зон (анализ сцены) треков не касаются — их допуск не растягивает буфер.
        max_gap = max(
            (
                r.params.gap_tolerance_s
                for r in self.rules
                if not r.instance.requires_scene_analysis
            ),
            default=1.0,
        )
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
            event_id_seed=event_id_seed,
        )

    @staticmethod
    def _build_rules(
        config: SourceConfig,
    ) -> tuple[dict[str, ZoneGeometry], list[_ActiveRule], dict[str, RuleContext]]:
        zones = {
            z.id: ZoneGeometry(id=z.id, name=z.name, polygon=[tuple(p) for p in z.polygon])
            for z in config.zones
        }
        rules: list[_ActiveRule] = []
        contexts: dict[str, RuleContext] = {}
        for rule in config.rules:
            try:
                cls = get_scenario(rule.scenario_type)
            except KeyError:
                logger.warning(
                    "Правило %s: сценарий %s не найден — пропускаем", rule.id, rule.scenario_type
                )
                continue
            params = cls.ParamsModel.from_stored(rule.params)
            if rule.zone_id:
                rule_zones = [zones[rule.zone_id]]
            else:
                # Правило без зоны: сценарию, которому зона обязательна, — все зоны источника;
                # сценарию без обязательной зоны — весь кадр (пустой список зон).
                rule_zones = list(zones.values()) if cls.requires_zone else []
            rules.append(_ActiveRule(cls(rule.id), params, rule_zones))
            contexts[rule.id] = RuleContext(
                rule_id=rule.id,
                scenario_type=cls.scenario_type,
                title=cls.title,
                priority=Priority(rule.priority),
                version=rule.version,
                params=params,
                zone_ids=tuple(z.id for z in rule_zones),
            )
        return zones, rules, contexts

    def request_config(self, config: SourceConfig) -> None:
        """Из другого потока: применить конфигурацию перед следующим кадром (в потоке конвейера)."""
        self._pending_config = config

    def apply_config(self, config: SourceConfig) -> None:
        """Новые зоны и правила на лету (воркер перечитывает конфигурацию). Детектор, трекер и
        чтение кадров не перезапускаются; эпизоды удалённых правил завершаются, прочие идут."""
        self.config = config
        self.zones, self.rules, contexts = self._build_rules(config)
        self.episodes.reconfigure(contexts, self.zones)
        self._build_scene()
        logger.info("Конфигурация обновлена: правил %s, зон %s", len(self.rules), len(self.zones))

    def process_frame(self, frame: Frame) -> FrameResult:
        """Один кадр: детектор, трекер и анализ сцены — по одному разу; затем все правила читают
        общий FrameContext; EpisodeManager собирает эпизоды."""
        t0 = time.perf_counter()
        dets = self.detector.infer(frame)
        if not dets.analyzed:
            # Нет данных детектора: трекер не обновляется (иначе люди «исчезли бы»), правила не
            # вызываются, эпизоды и зоны получают «оценить нельзя».
            self.stats.record_unanalyzed()
            self.episodes.process_unanalyzed(
                frame, dets.unanalyzed_reason or "кадр не проанализирован"
            )
            self.stats.record_frame_ms((time.perf_counter() - t0) * 1000)
            return FrameResult([], [], set(), zone_states=self.episodes.zone_states())
        self.stats.record(dets.inference_ms, dets.preprocess_ms, dets.postprocess_ms)
        tracks = self.tracker.update(dets)
        confirmed = [t for t in tracks if t.state == "confirmed"]
        context = FrameContext(
            frame=frame,
            video_ts=frame.video_ts,
            confirmed_tracks=confirmed,
            all_tracks=tracks,
            detections=dets,
            scene=self._analyze_scene(frame, tracks, dets),
        )
        hits: list[RuleHit] = []
        info: dict[int, dict] = {}
        for rule in self.rules:
            hits.extend(rule.instance.update(context, rule.zones, rule.params))
            for tid, data in rule.instance.describe(context, rule.zones, rule.params).items():
                info.setdefault(tid, {}).update(data)
        self.episodes.process(frame, hits, confirmed, tracks)
        self.stats.record_frame_ms((time.perf_counter() - t0) * 1000)
        return FrameResult(
            tracks,
            hits,
            self.episodes.alerting_track_ids(),
            self.episodes.alerting_scenarios(),
            info,
            self.episodes.candidate_track_ids(),
            self.episodes.zone_states(),
        )

    def _build_scene(self) -> None:
        """SceneAnalyzer — только если он нужен хотя бы одному правилу, и только для их зон.
        Пересоздаётся, лишь когда изменились эти зоны, их параметры или эталоны."""
        scene_zones: dict[str, tuple[ZoneGeometry, SceneParams]] = {}
        for rule in self.rules:
            if not rule.instance.requires_scene_analysis:
                continue
            make = getattr(rule.params, "scene_params", None)
            for zone in rule.zones:
                scene_zones.setdefault(zone.id, (zone, make() if make else SceneParams()))
        refs = {z.id: z.reference_url for z in self.config.zones if z.id in scene_zones}
        key = (
            tuple(sorted((k, repr(v)) for k, v in scene_zones.items())),
            tuple(sorted(refs.items())),
        )
        if key == self._scene_key:
            return
        self._scene_key = key
        if not scene_zones:
            self.scene = None
            return
        references = {}
        if self.reference_loader is not None:
            for zone in self.config.zones:
                if zone.id in scene_zones and zone.reference_url:
                    image = self.reference_loader(zone)
                    if image is not None:
                        references[zone.id] = image
        self.scene = SceneAnalyzer(
            scene_zones, hz=self.scene_hz, bootstrap_s=self.scene_bootstrap_s, references=references
        )

    def _analyze_scene(self, frame: Frame, tracks: list[Track], dets) -> dict | None:
        """Анализ сцены (модель фона) — только если он нужен хотя бы одному правилу."""
        if self.scene is None:
            return None
        result = self.scene.update(frame, tracks, dets)
        if self.scene.last_ms is not None:
            self.stats.record_scene(self.scene.last_ms)
        return result

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
                pending, self._pending_config = self._pending_config, None
                if pending is not None:
                    self.apply_config(pending)
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
