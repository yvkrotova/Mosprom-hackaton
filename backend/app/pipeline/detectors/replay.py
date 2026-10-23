"""ReplayDetector: детекции из JSONL-файла (одна строка = FrameDetections, см. docs/CV_CONTRACT.md).

Так CV-участник подключает модель, которая пока работает только локально:
прогнал видео моделью → сохранил JSONL → запустил backend с DETECTOR=replay.
"""

import bisect
import json
from pathlib import Path

from pydantic import ValidationError

from app.pipeline.interfaces import Detector, Frame, FrameDetections


class ReplayFileError(ValueError):
    pass


def load_replay_file(path: Path) -> list[FrameDetections]:
    """Читает и проверяет файл; ошибка содержит номер строки."""
    if not path.is_file():
        raise ReplayFileError(f"Файл детекций не найден: {path}")
    records: list[FrameDetections] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                records.append(FrameDetections.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ReplayFileError(f"{path}:{lineno}: {exc}") from exc
    if not records:
        raise ReplayFileError(f"Файл детекций пуст: {path}")
    return sorted(records, key=lambda r: r.video_ts)


class ReplayDetector(Detector):
    kind = "replay"

    def __init__(self, path: str | Path, analysis_fps: float = 10.0) -> None:
        self.records = load_replay_file(Path(path))
        self._ts = [r.video_ts for r in self.records]
        self.model_version = self.records[0].model_version
        # Запись подходит кадру, если ближе половины интервала между кадрами анализа.
        self.tolerance_s = 0.5 / analysis_fps

    def _nearest(self, ts: float) -> FrameDetections | None:
        i = bisect.bisect_left(self._ts, ts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self._ts) and (
                best is None or abs(self._ts[j] - ts) < abs(self._ts[best] - ts)
            ):
                best = j
        if best is None or abs(self._ts[best] - ts) > self.tolerance_s + 1e-9:
            return None
        return self.records[best]

    def infer(self, frame: Frame) -> FrameDetections:
        record = self._nearest(frame.video_ts)
        return FrameDetections(
            source_id=frame.source_id,
            frame_idx=frame.frame_idx,
            video_ts=frame.video_ts,
            frame_w=record.frame_w if record else frame.width,
            frame_h=record.frame_h if record else frame.height,
            inference_ms=record.inference_ms if record else 0.0,
            model_version=self.model_version,
            detections=list(record.detections) if record else [],
        )
