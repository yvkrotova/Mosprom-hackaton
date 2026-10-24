"""Замер скорости детектора: прогрев, затем avg/p50/p95 инференса, пре- и постпроцессинга, FPS.

    python tools/benchmark_detector.py [video.mp4] [--detector onnx] [--frames 200] [--warmup 10]
                                       [--out benchmark.json]

Без видео кадры синтетические (1280x720). Результат печатается и сохраняется в JSON — его можно
положить в отчёт рядом с теми же замерами на LinQ (--detector linq).
- inference_ms — только вызов сети (session.run / программа LinQ);
- preprocess_ms / postprocess_ms — letterbox и декодирование + NMS;
- fps — кадров в секунду по полному времени infer() (без чтения видео).
"""

import argparse
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.pipeline.detectors import DETECTOR_CHOICES, build_detector  # noqa: E402
from app.pipeline.interfaces import Detector, Frame  # noqa: E402
from app.pipeline.sources import VideoFileSource  # noqa: E402


def _frames(video: Path | None, count: int, fps: float) -> list[Frame]:
    if video is None:
        rng = np.random.default_rng(0)
        image = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        return [Frame("bench", i, i / fps, 1280, 720, image) for i in range(count)]
    frames = []
    for frame in VideoFileSource("bench", str(video), fps):
        frames.append(frame)
        if len(frames) >= count:
            break
    if not frames:
        raise ValueError(f"Из видео не прочитано ни одного кадра: {video}")
    return frames


def _summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    arr = np.array(values)
    return {
        "avg": round(float(arr.mean()), 3),
        "p50": round(float(np.percentile(arr, 50)), 3),
        "p95": round(float(np.percentile(arr, 95)), 3),
        "min": round(float(arr.min()), 3),
        "max": round(float(arr.max()), 3),
    }


def benchmark(detector: Detector, frames: list[Frame], warmup: int, runs: int) -> dict:
    for i in range(warmup):
        detector.infer(frames[i % len(frames)])
    inference, pre, post, total, det_count = [], [], [], [], 0
    for i in range(runs):
        t0 = time.perf_counter()
        dets = detector.infer(frames[i % len(frames)])
        total.append((time.perf_counter() - t0) * 1000)
        inference.append(dets.inference_ms)
        if dets.preprocess_ms is not None:
            pre.append(dets.preprocess_ms)
        if dets.postprocess_ms is not None:
            post.append(dets.postprocess_ms)
        det_count += len(dets.detections)
    return {
        "detector_kind": detector.kind,
        "model_version": detector.model_version,
        "frames": runs,
        "warmup": warmup,
        "frame_size": [frames[0].width, frames[0].height],
        "inference_ms": _summary(inference),
        "preprocess_ms": _summary(pre),
        "postprocess_ms": _summary(post),
        "total_ms": _summary(total),
        "fps": round(1000 * runs / sum(total), 2) if total else None,
        "detections_per_frame": round(det_count / runs, 2) if runs else None,
    }


def _environment() -> dict:
    env = {"python": platform.python_version(), "platform": platform.platform(),
           "processor": platform.processor() or platform.machine()}  # fmt: skip
    try:
        import onnxruntime

        env["onnxruntime"] = onnxruntime.__version__
    except ImportError:
        pass
    return env


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("video", type=Path, nargs="?", help="Видео; без него — синтетические кадры")
    parser.add_argument("--detector", default="onnx", choices=DETECTOR_CHOICES)
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--fps", type=float, default=settings.default_analysis_fps)
    parser.add_argument("--out", type=Path, default=Path("benchmark.json"))
    args = parser.parse_args()

    if args.video is not None and not args.video.is_file():
        print(f"Видео не найдено: {args.video}")
        return 1
    frames = _frames(args.video, min(args.frames, 300), args.fps)
    detector = build_detector(args.detector, settings, analysis_fps=args.fps)
    try:
        result = benchmark(detector, frames, args.warmup, args.frames)
    finally:
        detector.close()
    result["source"] = str(args.video) if args.video else "synthetic 1280x720"
    result["environment"] = _environment()
    result["measured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if result["detector_kind"] != "linq":
        print(f"\nВНИМАНИЕ: детектор {result['detector_kind']} — это НЕ замер на LinQ.")
    args.out.write_text(text + "\n", encoding="utf-8")
    print(f"Сохранено: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
