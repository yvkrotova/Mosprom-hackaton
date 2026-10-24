"""Воркер одного источника: конфигурация из backend -> конвейер -> события и heartbeat.

Запускается из workers/run_pipeline.py (отдельный процесс). Вынесен в функцию, чтобы
его можно было прогнать в тестах внутри процесса.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.config import Settings
from app.pipeline.annotate import annotate_frame
from app.pipeline.backend_client import BackendClient
from app.pipeline.detectors import build_detector, detector_kind_for
from app.pipeline.detectors.replay import ReplayDetector
from app.pipeline.interfaces import Detector, Frame, FrameSource
from app.pipeline.live import LiveSender, frame_meta
from app.pipeline.runner import FrameResult, Pipeline, PipelineStats
from app.pipeline.sources import (
    NullFrameSource,
    VideoFileSource,
    analysis_frame_indices,
    probe_video,
)
from app.schemas.enums import SourceKind
from app.schemas.internal import SourceConfig

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PIPELINE_ERROR = 1
EXIT_CONFIG_ERROR = 2


def build_frame_source(config: SourceConfig, detector: Detector, realtime: bool) -> FrameSource:
    src = config.source
    if src.kind == SourceKind.IMAGES:
        raise ValueError("Источники kind=images пока не поддерживаются")
    if isinstance(detector, ReplayDetector) and detector.legacy and not Path(src.uri).exists():
        # Старая запись (v1) без видео: кадры без картинки на всю длину файла детекций.
        logger.warning("Видео %s не найдено — replay без кадров-доказательств", src.uri)
        last_ts = detector.read_record(detector.last_frame_idx)["video_ts"]
        return NullFrameSource(src.id, last_ts, src.analysis_fps)
    return VideoFileSource(
        src.id,
        src.uri,
        src.analysis_fps,
        is_stream=src.kind == SourceKind.RTSP,
        realtime=realtime,
    )


def make_detector(config: SourceConfig, settings: Settings, detector_kind: str) -> Detector:
    """Источник с привязанной записью — ReplayDetector (с проверкой соответствия видео и
    расписания кадров); иначе детектор из настройки DETECTOR."""
    src = config.source
    if config.replay is None:
        if detector_kind == "replay":
            raise ValueError(
                "DETECTOR=replay, но для источника не найдена запись детекций: "
                + (config.replay_note or "задайте replay_path источника")
            )
        return build_detector(
            detector_kind,
            settings,
            zone_polygon=config.zones[0].polygon if config.zones else None,
            analysis_fps=src.analysis_fps,
        )
    detector = ReplayDetector(
        config.replay.path, src.analysis_fps, on_missing=settings.replay_on_missing
    )
    try:
        if not detector.legacy:
            if not Path(src.uri).is_file():
                raise ValueError(f"Видео источника не найдено: {src.uri}")
            info = probe_video(src.uri)
            detector.verify_video(sha256=config.replay.video_sha256, **info)
            if info["fps"] and info["frame_count"]:
                detector.check_schedule(
                    analysis_frame_indices(info["fps"], info["frame_count"], src.analysis_fps)
                )
    except Exception:
        detector.close()
        raise
    logger.info(
        "Replay: %s (%s), источник записи — %s", detector.path, detector.kind, config.replay.origin
    )
    return detector


class HeartbeatThread(threading.Thread):
    """Шлёт status=running со свежими метриками каждые interval_s, пока не остановлен."""

    def __init__(self, send, interval_s: float) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self._send = send
        self._interval_s = interval_s
        self._stopped = threading.Event()

    def run(self) -> None:
        while True:
            self._send("running", None)
            if self._stopped.wait(self._interval_s):
                return

    def stop(self) -> None:
        self._stopped.set()
        if self.is_alive():
            self.join(timeout=self._interval_s + 5)


def load_reference(settings: Settings, url: str | None):
    """Эталон пустого прохода зоны (файл в STORAGE_DIR) как BGR-кадр или None."""
    if not url:
        return None
    path = settings.storage_dir / url.removeprefix("/media/")
    try:
        return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        logger.warning("Эталон зоны не прочитан: %s", path)
        return None


def config_fingerprint(config: SourceConfig) -> str:
    """Что влияет на анализ: зоны и правила (статус источника и heartbeat — нет)."""
    zones = [z.model_dump(mode="json", exclude={"created_at", "updated_at"}) for z in config.zones]
    rules = [r.model_dump(mode="json", exclude={"created_at", "updated_at"}) for r in config.rules]
    return json.dumps({"zones": zones, "rules": rules}, sort_keys=True)


class ConfigWatcher(threading.Thread):
    """Раз в interval_s перечитывает конфигурацию источника; изменилась — отдаёт её конвейеру.
    Так правки зон и правил у запущенного источника применяются без перезапуска воркера
    (перезапуск начал бы видеофайл сначала и повторил бы тревоги)."""

    def __init__(self, load, apply, first: SourceConfig, interval_s: float) -> None:
        super().__init__(name="config-watcher", daemon=True)
        self._load, self._apply = load, apply
        self._fingerprint = config_fingerprint(first)
        self._interval_s = interval_s
        self._stopped = threading.Event()

    def check(self) -> bool:
        """Одна проверка. True — конфигурация изменилась и передана конвейеру."""
        try:
            config = self._load()
        except Exception as exc:  # backend недоступен — попробуем в следующий раз
            logger.warning("Конфигурация не перечитана: %s", exc)
            return False
        fingerprint = config_fingerprint(config)
        if fingerprint == self._fingerprint:
            return False
        self._fingerprint = fingerprint
        self._apply(config)
        return True

    def run(self) -> None:
        while not self._stopped.wait(self._interval_s):
            self.check()

    def stop(self) -> None:
        self._stopped.set()


def run_worker(
    source_id: str,
    client: BackendClient,
    settings: Settings,
    detector_kind: str,
    stop_event: threading.Event,
    *,
    realtime: bool = False,
) -> int:
    """Возвращает код выхода процесса. Любая ошибка детектора или источника -> status=error."""
    state: dict[str, Any] = {
        "stats": None,
        "model_version": None,
        # До создания детектора — по настройке; потом — Detector.kind (onnx -> onnx_cpu).
        "detector_kind": detector_kind_for(detector_kind),
        "replay_file": None,
    }

    def send_heartbeat(status: str, error: str | None) -> None:
        stats: PipelineStats | None = state["stats"]
        payload = {
            "source_id": source_id,
            "status": status,
            "fps": stats.fps if stats else None,
            "inference_ms_avg": stats.inference_ms_avg if stats else None,
            "inference_ms_p95": stats.inference_ms_p95 if stats else None,
            "preprocess_ms_avg": stats.preprocess_ms_avg if stats else None,
            "postprocess_ms_avg": stats.postprocess_ms_avg if stats else None,
            "scene_ms_avg": stats.scene_ms_avg if stats else None,
            "frame_ms_avg": stats.frame_ms_avg if stats else None,
            "frames_unanalyzed": stats.frames_unanalyzed
            if stats and stats.recorded_timings
            else None,
            "replay_file": state["replay_file"],
            **(
                {f"recorded_inference_ms_{k}": v for k, v in stats.recorded_inference().items()}
                if stats and stats.recorded_timings
                else {}
            ),
            "frames_processed": stats.frames_processed if stats else 0,
            "model_version": state["model_version"],
            "detector_kind": state["detector_kind"],
            "error": error,
        }
        try:
            client.heartbeat(payload)
        except Exception as exc:  # heartbeat не должен ронять обработку
            logger.warning("Heartbeat не отправлен: %s", exc)

    try:
        config = client.get_config(source_id)
    except Exception as exc:
        logger.exception("Не удалось получить конфигурацию источника %s", source_id)
        send_heartbeat("error", f"Не удалось получить конфигурацию: {exc}")
        return EXIT_CONFIG_ERROR

    if not config.rules:
        logger.warning("У источника нет включённых правил — нарушения искаться не будут")

    heartbeat = HeartbeatThread(send_heartbeat, config.settings.heartbeat_interval_s)
    live = LiveSender(
        lambda meta, image: client.send_live(source_id, meta, image),
        max_rate_hz=settings.tracks_push_hz,
    )
    detector: Detector | None = None
    watcher: ConfigWatcher | None = None
    status, error, code = "stopped", None, EXIT_OK
    try:
        detector = make_detector(config, settings, detector_kind)
        state["model_version"] = detector.model_version
        state["detector_kind"] = detector.kind
        if isinstance(detector, ReplayDetector):
            state["replay_file"] = detector.path.name
        frames = build_frame_source(config, detector, realtime)
        pipeline = Pipeline(
            config,
            detector,
            client,
            # Replay: повторный прогон той же записи даёт те же event_id (журнал без дублей).
            event_id_seed=detector.jsonl_sha256 if isinstance(detector, ReplayDetector) else None,
            tracker_config=settings.tracker_config,
            scene_hz=settings.scene_analysis_hz,
            scene_bootstrap_s=settings.scene_bootstrap_s,
            reference_loader=lambda zone: load_reference(settings, zone.reference_url),
        )
        state["stats"] = pipeline.stats
        watcher = ConfigWatcher(
            lambda: client.get_config(source_id),
            pipeline.request_config,
            config,
            settings.config_reload_s,
        )
        watcher.start()
        heartbeat.start()
        live.start()
        logger.info("Старт: источник %s, детектор %s (%s), трекер %s", config.source.name,
                    detector.kind, detector.model_version, settings.tracker)  # fmt: skip

        def on_frame(frame: Frame, result: FrameResult) -> None:
            meta = frame_meta(frame, result, pipeline.stats.fps, detector.kind)

            annotated = annotate_frame(
                image=frame.image,
                width=frame.width,
                height=frame.height,
                zones=config.zones,
                tracks=result.tracks,
                violator_track_id=None,
                violation_zone_id=None,
                caption_lines=[],
            )

            live.submit(meta, annotated)

        pipeline.run(frames, should_stop=stop_event.is_set, on_frame=on_frame)
        logger.info(
            "Готово: %s кадров, эпизодов %s%s",
            pipeline.stats.frames_processed,
            pipeline.episodes.episodes_confirmed,
            " (остановлено по запросу)" if stop_event.is_set() else "",
        )
    except Exception as exc:
        logger.exception("Ошибка конвейера")
        status, error, code = "error", f"{type(exc).__name__}: {exc}", EXIT_PIPELINE_ERROR
    finally:
        if watcher is not None:
            watcher.stop()
        heartbeat.stop()
        live.stop()
        if detector is not None:
            try:
                detector.close()
            except Exception:
                logger.exception("Ошибка при закрытии детектора")
        send_heartbeat(status, error)
    return code
