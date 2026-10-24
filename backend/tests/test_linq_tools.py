"""Инструменты LinQ без SDK: сравнение детекций с ONNX и сводка замеров по записи с TPU.

Тесты с реальным сервисом LinQ пропускаются, пока в окружении нет LINQ_* (доступ только из Jupyter).
"""

import importlib.util
import json
import os

import pytest

from app.config import BACKEND_DIR

LINQ_DIR = BACKEND_DIR.parent / "linq"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(name, LINQ_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(idx, boxes, kind, ms, **extra):
    return {
        "frame_idx": idx,
        "video_ts": idx / 10,
        "frame_w": 1280,
        "frame_h": 720,
        "inference_ms": ms,
        "model_version": kind,
        "detector_kind": kind,
        **extra,
        "detections": [{"label": "person", "confidence": c, "bbox": b} for b, c in boxes],
    }


def _write(path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return path


def test_compare_with_onnx_counts_matches_misses_extras(tmp_path):
    mod = _module("compare_with_onnx")
    a, b = [0.1, 0.1, 0.2, 0.5], [0.6, 0.1, 0.7, 0.5]
    onnx = _write(tmp_path / "o.jsonl", [_rec(0, [(a, 0.9), (b, 0.8)], "onnx_cpu", 70),
                                         _rec(1, [(a, 0.9)], "onnx_cpu", 70)])  # fmt: skip
    shifted = [0.11, 0.1, 0.21, 0.5]  # почти тот же бокс
    linq = _write(
        tmp_path / "l.jsonl",
        [
            _rec(0, [(shifted, 0.85)], "linq_tpu", 9),
            _rec(1, [(a, 0.88), ([0.8, 0.8, 0.9, 0.99], 0.5)], "linq_tpu", 9),
        ],
    )
    r = mod.compare(mod.load(onnx), mod.load(linq))
    assert (r["onnx_boxes"], r["matched"], r["missed_by_linq"], r["extra_in_linq"]) == (3, 2, 1, 1)
    assert r["match_rate"] == pytest.approx(0.667, abs=1e-3) and r["mean_iou"] > 0.8
    assert r["inference_ms_avg"] == {"onnx": 70.0, "linq": 9.0}


def test_benchmark_linq_separates_network_and_device(tmp_path):
    mod = _module("benchmark_linq")
    recs = [
        _rec(i, [], "linq_tpu", 20.0, device_ms=8.0, preprocess_ms=3.0, postprocess_ms=1.0)
        for i in range(10)
    ]
    r = mod.summarize(_write(tmp_path / "l.jsonl", recs), warmup=2)
    assert (
        r["inference_ms"]["n"] == 8
        and r["device_ms"]["avg"] == 8.0
        and r["network_ms"]["avg"] == 12.0
    )
    assert r["fps_estimate"] == pytest.approx(1000 / 24, abs=0.01)


@pytest.mark.skipif(
    not os.environ.get("LINQ_API_URL"), reason="нет доступа к сервису LinQ (LINQ_* не заданы)"
)
def test_linq_service_smoke():
    pytest.skip("вызов LinQ ещё не реализован: ждём usage_example_python_api.zip")
