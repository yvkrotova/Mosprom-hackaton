"""Интеграция на реальных тестовых роликах (data/videos). Пропускается, если файлов или модели нет.

Детекции кэшируются в data/videos/cache (первый прогон ONNX на CPU — ~10 с на ролик).
"""

import pytest

from app.config import BACKEND_DIR
from tools.videos import manifest, video_path

MODEL = BACKEND_DIR / "models" / "yolov8s.onnx"


def _entry(video_id: str) -> dict:
    entry = next((v for v in manifest() if v["id"] == video_id), None)
    if entry is None or not video_path(entry).is_file() or not MODEL.is_file():
        pytest.skip(f"нет ролика {video_id} или модели")
    pytest.importorskip("onnxruntime")
    return entry


def _evaluate(entry):
    from app.pipeline.runner import Pipeline
    from tools.evaluate_episodes import Sink, source_config
    from tools.videos import CachedDetector, cached_detections, frames_of

    records = cached_detections(entry, "onnx", 10.0)
    cfg = source_config(entry, 10.0)
    sink = Sink()
    Pipeline(cfg, CachedDetector(records), sink).run(frames_of(records, cfg.source.id))
    return list(sink.final.values())


def test_kth_running_gives_one_alert_per_crossing():
    episodes = _evaluate(_entry("kth_running"))
    runs = [e for e in episodes if e["scenario_type"] == "run_fast_movement"]
    assert len(runs) == 4
    assert all(1.8 <= e["details"]["speed_max"] <= 2.6 for e in runs)


def test_factory_walkers_do_not_trigger_running():
    for vid in ("te1", "te21"):
        episodes = _evaluate(_entry(vid))
        assert not [e for e in episodes if e["scenario_type"] == "run_fast_movement"], vid
        assert len([e for e in episodes if e["scenario_type"] == "danger_zone"]) == 1, vid
