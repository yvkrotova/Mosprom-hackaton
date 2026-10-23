"""Воркер одного источника: конфигурация из backend -> конвейер -> события и heartbeat.

Запускается из workers/run_pipeline.py (отдельный процесс). Вынесен в функцию, чтобы
его можно было прогнать в тестах внутри процесса.
"""

import logging
import threading
from pathlib import Path
from typing import Any

from app.config import Settings
from app.pipeline.backend_client import BackendClient
from app.pipeline.detectors import build_detector
from app.pipeline.detectors.replay import ReplayDetector
from app.pipeline.interfaces import Detector, Frame, FrameSource
from app.pipeline.live import LiveSender, frame_meta
from app.pipeline.runner import FrameResult, Pipeline, PipelineStats
from app.pipeline.sources import NullFrameSource, VideoFileSource
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
    if isinstance(detector, ReplayDetector) and not Path(src.uri).exists():
        # Replay без видео: кадры без картинки на всю длину файла детекций.
        logger.warning("Видео %s не найдено — replay без кадров-доказательств", src.uri)
        return NullFrameSource(src.id, detector.records[-1].video_ts, src.analysis_fps)
    return VideoFileSource(
        src.id,
        src.uri,
        src.analysis_fps,
        is_stream=src.kind == SourceKind.RTSP,
        realtime=realtime,
    )


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
    state: dict[str, Any] = {"stats": None, "model_version": None}

    def send_heartbeat(status: str, error: str | None) -> None:
        stats: PipelineStats | None = state["stats"]
        payload = {
            "source_id": source_id,
            "status": status,
            "fps": stats.fps if stats else None,
            "inference_ms_avg": stats.inference_ms_avg if stats else None,
            "inference_ms_p95": stats.inference_ms_p95 if stats else None,
            "frames_processed": stats.frames_processed if stats else 0,
            "model_version": state["model_version"],
            "detector_kind": detector_kind,
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
    live = LiveSender(lambda meta, image: client.send_live(source_id, meta, image))
    detector: Detector | None = None
    status, error, code = "stopped", None, EXIT_OK
    try:
        detector = build_detector(
            detector_kind,
            settings,
            zone_polygon=config.zones[0].polygon if config.zones else None,
            analysis_fps=config.source.analysis_fps,
        )
        state["model_version"] = detector.model_version
        frames = build_frame_source(config, detector, realtime)
        pipeline = Pipeline(config, detector, client)
        state["stats"] = pipeline.stats
        heartbeat.start()
        live.start()
        logger.info("Старт: источник %s, детектор %s (%s)", config.source.name, detector.kind,
                    detector.model_version)  # fmt: skip

        def on_frame(frame: Frame, result: FrameResult) -> None:
            live.submit(frame_meta(frame, result, pipeline.stats.fps), frame.image)

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
        heartbeat.stop()
        live.stop()
        if detector is not None:
            try:
                detector.close()
            except Exception:
                logger.exception("Ошибка при закрытии детектора")
        send_heartbeat(status, error)
    return code
