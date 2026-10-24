"""Выбор детектора: DETECTOR=mock | replay | onnx | linq. Остальной конвейер от него не зависит."""

from collections.abc import Sequence

from app.config import Settings
from app.pipeline.interfaces import Detector

DETECTOR_CHOICES = ("mock", "replay", "onnx", "linq")

# Значение DETECTOR -> Detector.kind, который виден в /health, /metrics и на фронте.
# onnx_cpu — локальный CPU, а не LinQ: на демо это должно быть видно честно.
DETECTOR_KINDS = {"mock": "mock", "replay": "replay", "onnx": "onnx_cpu", "linq": "linq_tpu"}


def detector_kind_for(setting: str) -> str:
    return DETECTOR_KINDS.get(setting, setting)


def build_detector(
    kind: str,
    settings: Settings,
    *,
    zone_polygon: Sequence[Sequence[float]] | None = None,
    analysis_fps: float = 10.0,
) -> Detector:
    if kind == "mock":
        if settings.mock_scenario == "run_fast_movement":
            from app.pipeline.detectors.mock_run import MockRunDetector

            return MockRunDetector()
        from app.pipeline.detectors.mock import MockDetector

        return MockDetector(zone_polygon)
    if kind == "replay":
        from app.pipeline.detectors.replay import ReplayDetector

        if not settings.replay_path:
            raise ValueError("DETECTOR=replay, но REPLAY_PATH не задан")
        return ReplayDetector(
            settings.replay_path, analysis_fps, on_missing=settings.replay_on_missing
        )
    if kind == "onnx":
        from app.pipeline.detectors.onnx import OnnxDetector

        return OnnxDetector(settings)
    if kind == "linq":
        if settings.linq_bridge_url:
            from app.pipeline.detectors.linq_bridge import LinqBridgeDetector

            return LinqBridgeDetector(settings)
        from app.pipeline.detectors.linq import LinqDetector

        return LinqDetector(settings)
    raise ValueError(f"Неизвестный детектор: {kind!r} (ожидается {', '.join(DETECTOR_CHOICES)})")
