"""Общее для инструментов проверки на тестовых видео: манифест, кэш детекций, прогон конвейера.

Детекции дорогие (ONNX на CPU ~70 мс/кадр), поэтому каждый ролик прогоняется детектором один раз
на пару (детектор, частота) и сохраняется в data/videos/cache/<id>_<detector>_<fps>fps.jsonl —
тот же формат FrameDetections, что у DETECTOR=replay. Дальше трекеры и правила гоняются по кэшу.
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BACKEND_DIR, Settings  # noqa: E402
from app.pipeline.detectors import build_detector  # noqa: E402
from app.pipeline.interfaces import Detector, Frame, FrameDetections  # noqa: E402
from app.pipeline.sources import VideoFileSource  # noqa: E402

VIDEOS_DIR = BACKEND_DIR / "data" / "videos"
MANIFEST = VIDEOS_DIR / "manifest.json"
LABELS = VIDEOS_DIR / "labels.json"
CACHE = VIDEOS_DIR / "cache"


def manifest() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["videos"]


def labels() -> dict[str, list[dict]]:
    return json.loads(LABELS.read_text(encoding="utf-8"))["videos"]


def video_path(entry: dict) -> Path:
    return BACKEND_DIR / entry["file"]


def select(ids: list[str] | None) -> list[dict]:
    """Ролики манифеста по id (все, если ids пусто); отсутствующие файлы пропускаются."""
    out = []
    for v in manifest():
        if ids and v["id"] not in ids:
            continue
        if not video_path(v).is_file():
            print(f"  пропуск {v['id']}: нет файла {v['file']}")
            continue
        out.append(v)
    return out


def cached_detections(
    entry: dict, detector: str = "onnx", fps: float = 10.0, settings: Settings | None = None
) -> list[FrameDetections]:
    """Детекции ролика (из кэша или с прогоном детектора). Порог — как в конвейере (низкий)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{entry['id']}_{detector}_{fps:g}fps.jsonl"
    if path.is_file():
        return [
            FrameDetections.model_validate_json(line)
            for line in path.read_text("utf-8").splitlines()
        ]
    settings = settings or Settings(_env_file=None)
    det = build_detector(detector, settings, analysis_fps=fps)
    records = []
    try:
        for frame in VideoFileSource(entry["id"], str(video_path(entry)), fps):
            rec = det.infer(frame)
            rec.detector_kind = det.kind
            records.append(rec)
    finally:
        det.close()
    path.write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")
    print(
        f"  детекции {entry['id']} ({detector}, {fps:g} кадр/с): "
        f"{len(records)} кадров -> {path.name}"
    )
    return records


class CachedDetector(Detector):
    """Отдаёт заранее посчитанные детекции по порядку кадров."""

    def __init__(self, records: list[FrameDetections]) -> None:
        self.records = records
        self.kind = records[0].detector_kind or "replay" if records else "replay"
        self.model_version = records[0].model_version if records else "cache"
        self._by_idx = {r.frame_idx: r for r in records}

    def infer(self, frame: Frame) -> FrameDetections:
        rec = self._by_idx[frame.frame_idx]
        return rec.model_copy(update={"source_id": frame.source_id})


def frames_of(records: list[FrameDetections], source_id: str = "eval") -> Iterator[Frame]:
    """Кадры без картинки с теми же frame_idx и video_ts, что в кэше детекций."""
    for r in records:
        yield Frame(source_id, r.frame_idx, r.video_ts, r.frame_w, r.frame_h, None)


def frames_with_images(entry: dict, fps: float) -> Iterator[Frame]:
    yield from VideoFileSource(entry["id"], str(video_path(entry)), fps)
