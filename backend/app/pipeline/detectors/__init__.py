"""Выбор детектора: DETECTOR=mock | replay | linq. Остальной конвейер от выбора не зависит."""

from collections.abc import Sequence

from app.config import Settings
from app.pipeline.interfaces import Detector


def build_detector(
    kind: str,
    settings: Settings,
    *,
    zone_polygon: Sequence[Sequence[float]] | None = None,
    analysis_fps: float = 10.0,
) -> Detector:
    if kind == "mock":
        from app.pipeline.detectors.mock import MockDetector

        return MockDetector(zone_polygon)
    if kind == "replay":
        from app.pipeline.detectors.replay import ReplayDetector

        if not settings.replay_path:
            raise ValueError("DETECTOR=replay, но REPLAY_PATH не задан")
        return ReplayDetector(settings.replay_path, analysis_fps)
    if kind == "linq":
        from app.pipeline.detectors.linq import LinqDetector

        return LinqDetector(settings)
    raise ValueError(f"Неизвестный детектор: {kind!r} (ожидается mock, replay или linq)")
