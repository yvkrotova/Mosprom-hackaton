"""Видео -> JSONL детекций (одна строка = FrameDetections, docs/CV_CONTRACT.md).

    python tools/export_detections.py shift.mp4 -o detections.jsonl [--fps 10] [--detector onnx]

Зачем:
- DETECTOR=replay: прогнать видео моделью один раз и потом показывать демо без нагрузки на CPU;
- сравнение с LinQ: те же кадры через --detector onnx и --detector linq -> два файла для сравнения.

Параметры модели (MODEL_PATH, CONF_THRESHOLD, IOU_THRESHOLD, DETECT_CLASSES) — из .env,
их можно переопределить флагами.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.pipeline.detectors import DETECTOR_CHOICES, build_detector  # noqa: E402
from app.pipeline.sources import VideoFileSource  # noqa: E402


def export(video: Path, out: Path, detector, fps: float, max_frames: int | None = None) -> int:
    """Пишет JSONL и возвращает число кадров."""
    frames = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for frame in VideoFileSource("export", str(video), fps):
            dets = detector.infer(frame)
            fh.write(dets.model_dump_json(exclude={"source_id"}) + "\n")
            frames += 1
            if max_frames and frames >= max_frames:
                break
    return frames


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--out", type=Path, required=True, help="Куда писать JSONL")
    parser.add_argument("--fps", type=float, default=settings.default_analysis_fps,
                        help="Частота анализа, кадр/с (как analysis_fps источника)")  # fmt: skip
    parser.add_argument("--detector", default="onnx", choices=DETECTOR_CHOICES)
    parser.add_argument("--model", help="MODEL_PATH")
    parser.add_argument("--conf", type=float, help="DETECTOR_CONF_THRESHOLD")
    parser.add_argument("--iou", type=float, help="IOU_THRESHOLD")
    parser.add_argument("--classes", help="DETECT_CLASSES, через запятую")
    parser.add_argument("--max-frames", type=int)
    args = parser.parse_args()

    if not args.video.is_file():
        print(f"Видео не найдено: {args.video}")
        return 1
    overrides = {"model_path": args.model, "detector_conf_threshold": args.conf,
                 "iou_threshold": args.iou, "detect_classes": args.classes}  # fmt: skip
    settings = settings.model_copy(update={k: v for k, v in overrides.items() if v is not None})
    detector = build_detector(args.detector, settings, analysis_fps=args.fps)
    print(f"Детектор: {detector.kind} ({detector.model_version})")
    t0 = time.perf_counter()
    try:
        frames = export(args.video, args.out, detector, args.fps, args.max_frames)
    finally:
        detector.close()
    spent = time.perf_counter() - t0
    print(f"Кадров: {frames} за {spent:.1f} с ({frames / spent:.1f} кадр/с) -> {args.out}")
    print(f"Проверка: python tools/validate_detections.py {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
