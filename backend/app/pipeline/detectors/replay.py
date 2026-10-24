"""ReplayDetector: предзаписанные детекции (JSONL) вместо живого инференса.

Типичный случай — результаты YOLOv8s, посчитанные на LinQ TPU скриптом linq/run_linq_detections.py
в Jupyter организаторов: видео → JSONL → backend проигрывает запись так, как будто детектор
работает сейчас. Трекер, правила и эпизоды не знают, откуда детекции.

Формат — replay_format.py (schema_version 2, рамки в пикселях). Запись выбирается по frame_idx
исходного видео через индекс смещений (файл не читается в память целиком). Различаются:
- запись есть, detections = [] — кадр обработан, людей нет;
- записи для кадра нет — ошибка источника (REPLAY_ON_MISSING=error) или кадр «не проанализирован»
  (unknown): правила и эпизоды трактуют его как «оценить нельзя», а не «нарушения нет»;
- запись закончилась раньше видео — то же, с отдельным текстом.
Записанное время инференса — историческое (на LinQ); искусственной задержки по нему нет.
"""

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from app.pipeline.detectors.replay_format import (
    LEGACY_LABEL,
    ReplayFormatError,
    load_index,
    stats_ms,
    validate_frame,
)
from app.pipeline.interfaces import Detection, Detector, Frame, FrameDetections

logger = logging.getLogger(__name__)

OnMissing = Literal["error", "unknown"]

# Допуски сверки с видео: FPS из контейнера бывает округлён по-разному.
FPS_TOLERANCE = 0.01


class ReplayFileError(ReplayFormatError):
    """Запись не подходит: повреждена, не от этого видео или несовместима с analysis_fps."""


class ReplayMissingFrame(RuntimeError):
    """Для кадра, который должен анализироваться, записи нет (REPLAY_ON_MISSING=error)."""


class ReplayDetector(Detector):
    kind = "replay"
    recorded_timings = True  # inference_ms — из записи, а не замер этого процесса

    def __init__(
        self,
        path: str | Path,
        analysis_fps: float = 10.0,
        *,
        on_missing: OnMissing = "error",
    ) -> None:
        self.path = Path(path)
        try:
            index = load_index(self.path)
        except ReplayFormatError as exc:
            raise ReplayFileError(str(exc)) from exc
        self.analysis_fps = analysis_fps
        self.on_missing = on_missing
        self.legacy: bool = index["legacy"]
        self.header: dict[str, Any] | None = index["header"]
        self.summary: dict[str, Any] | None = index["summary"]
        self.jsonl_sha256: str = index["jsonl_sha256"]
        self._offsets: dict[int, int] = {int(k): v for k, v in index["offsets"]}
        self.first_frame_idx = min(self._offsets)
        self.last_frame_idx = max(self._offsets)
        self._fh = self.path.open("rb")
        if self.legacy:
            first = self.read_record(self.first_frame_idx)
            self.frame_size = tuple(index["legacy_frame_size"] or (None, None))
            source_kind = index["legacy_detector_kind"]
            self.model_version = f"{first.get('model_version') or 'replay'} ({LEGACY_LABEL})"
            self.step = None
            logger.warning("Запись %s в старом формате (%s)", self.path.name, LEGACY_LABEL)
        else:
            header = self.header
            video = header["video"]
            self.frame_size = (video["width"], video["height"])
            source_kind = header["detector_kind"]
            model = header.get("model") or {}
            self.model_version = model.get("version") or model.get("name") or "unknown"
            self.step = header["step"]
        # Файл, посчитанный на LinQ, честно помечается как запись: linq_tpu -> linq_tpu_replay.
        self.kind = f"{source_kind}_replay" if source_kind else "replay"

    # --- сверка ----------------------------------------------------------------------------------

    def verify_video(
        self,
        *,
        sha256: str | None,
        fps: float | None,
        width: int | None,
        height: int | None,
        frame_count: int | None,
    ) -> None:
        """Запись от этого видео? Несовпадение — ошибка с перечнем различий."""
        if self.legacy:
            return  # в v1 нет метаданных — проверить нечем (это видно по model_version)
        video = self.header["video"]
        diffs = []
        if sha256 and video["sha256"] != sha256:
            diffs.append(f"SHA-256 видео {sha256[:12]}… ≠ {str(video['sha256'])[:12]}… в записи")
        if fps and abs(float(video["fps"]) - fps) > FPS_TOLERANCE * max(fps, 1):
            diffs.append(f"FPS {fps:g} ≠ {video['fps']:g} в записи")
        if width and height and (video["width"], video["height"]) != (width, height):
            diffs.append(f"разрешение {width}×{height} ≠ {video['width']}×{video['height']}")
        declared = video.get("frame_count_declared")
        if frame_count and declared and int(declared) != int(frame_count):
            diffs.append(f"кадров {frame_count} ≠ {declared} в записи")
        if diffs:
            raise ReplayFileError(
                f"Запись {self.path.name} не соответствует видео: " + "; ".join(diffs)
            )

    def check_schedule(self, frame_indices: Iterable[int]) -> None:
        """Кадры анализа должны быть подмножеством записанных. Запись сделана с шагом k, а
        analysis_fps требует другие кадры — ошибка при старте, а не рассинхронизация."""
        if self.legacy or not self.step or self.step == 1:
            return
        wanted = [i for i in frame_indices if i <= self.last_frame_idx]
        missing = [i for i in wanted if i not in self._offsets]
        if missing:
            raise ReplayFileError(
                f"analysis_fps источника несовместим с записью: записан каждый {self.step}-й "
                f"кадр, а анализ требует кадры {missing[:5]}… — задайте analysis_fps, при котором "
                f"берутся кадры с шагом {self.step}, или запишите детекции для каждого кадра"
            )

    # --- чтение ----------------------------------------------------------------------------------

    def read_record(self, frame_idx: int) -> dict[str, Any]:
        self._fh.seek(self._offsets[frame_idx])
        return json.loads(self._fh.readline())

    def missing_reason(self, frame_idx: int) -> str:
        if frame_idx > self.last_frame_idx:
            return (
                f"Запись {self.path.name} закончилась раньше видео: последний записанный кадр "
                f"{self.last_frame_idx}, видео продолжается (кадр {frame_idx})"
            )
        return f"В записи {self.path.name} нет кадра {frame_idx}, хотя он должен анализироваться"

    def infer(self, frame: Frame) -> FrameDetections:
        idx = frame.frame_idx
        if idx not in self._offsets:
            reason = self.missing_reason(idx)
            if self.on_missing == "error":
                raise ReplayMissingFrame(reason)
            return FrameDetections(
                source_id=frame.source_id,
                frame_idx=idx,
                video_ts=frame.video_ts,
                frame_w=frame.width,
                frame_h=frame.height,
                inference_ms=0.0,
                model_version=self.model_version,
                detector_kind=self.kind,
                analyzed=False,
                unanalyzed_reason=reason,
            )
        record = self.read_record(idx)
        w, h = self.frame_size
        if self.legacy:
            detections = [
                Detection.model_validate({k: v for k, v in d.items() if k != "track_id"})
                for d in record["detections"]
            ]
            w, h = record.get("frame_w") or frame.width, record.get("frame_h") or frame.height
        else:
            if frame.image is not None and (frame.width, frame.height) != (w, h):
                raise ReplayFileError(
                    f"Кадр {idx}: размер видео {frame.width}×{frame.height}, в записи {w}×{h}"
                )
            validate_frame(record, f"{self.path.name}: кадр {idx}", w, h)
            detections = [
                Detection(
                    label=d["label"],
                    confidence=d["confidence"],
                    bbox=(
                        min(1.0, max(0.0, d["bbox"][0] / w)),
                        min(1.0, max(0.0, d["bbox"][1] / h)),
                        min(1.0, max(0.0, d["bbox"][2] / w)),
                        min(1.0, max(0.0, d["bbox"][3] / h)),
                    ),
                )
                for d in record["detections"]
            ]
        return FrameDetections(
            source_id=frame.source_id,
            frame_idx=idx,
            video_ts=frame.video_ts,
            frame_w=w,
            frame_h=h,
            inference_ms=record["inference_ms"],
            preprocess_ms=record.get("preprocess_ms"),
            postprocess_ms=record.get("postprocess_ms"),
            device_ms=record.get("device_ms"),
            model_version=self.model_version,
            detector_kind=self.kind,
            detections=detections,
        )

    def recorded_inference_stats(self) -> dict[str, Any]:
        """Сводка из записи (историческое время на LinQ), если прогон её сохранил."""
        if self.summary and isinstance(self.summary.get("inference_ms"), dict):
            return self.summary["inference_ms"]
        return stats_ms([])

    def close(self) -> None:
        self._fh.close()
