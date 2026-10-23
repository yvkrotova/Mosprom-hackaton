"""Демо-видео, в котором «человек» ходит ровно там, где его «видит» MockDetector.

С DETECTOR=mock рамки на кадрах-доказательствах совпадают с фигурой. Зона размечена на полу
жёлтой линией. Ложный кадр MockDetector (15 с) на видео не рисуется — это ошибка «нейросети».

    python tools/make_demo_video.py [путь] [--size 1280x720] [--fps 25]
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.detectors.mock import (  # noqa: E402
    DURATION_S,
    FALSE_POSITIVE_TS,
    MockDetector,
)
from app.pipeline.interfaces import Frame  # noqa: E402

DEMO_ZONE = [[0.35, 0.45], [0.75, 0.45], [0.75, 0.95], [0.35, 0.95]]
DEFAULT_PATH = Path("data/demo/shift_demo.mp4")


def _background(w: int, h: int, zone: Sequence[Sequence[float]]) -> np.ndarray:
    img = np.zeros((h, w, 3), np.uint8)
    img[: int(h * 0.4)] = (70, 72, 75)  # стена
    img[int(h * 0.4) :] = (95, 100, 100)  # пол
    for x in range(0, w, w // 8):  # «колонны»
        cv2.rectangle(img, (x, 0), (x + w // 60, int(h * 0.4)), (55, 57, 60), -1)
    # Станок в зоне и разметка опасной зоны на полу (жёлто-чёрная линия).
    pts = np.array([[round(x * w), round(y * h)] for x, y in zone], np.int32)
    cx, cy = pts.mean(axis=0).astype(int)
    cv2.rectangle(img, (cx - w // 20, cy - h // 4), (cx + w // 20, cy - h // 12), (60, 60, 140), -1)
    cv2.polylines(img, [pts], True, (0, 210, 255), max(3, w // 200))
    cv2.polylines(img, [pts], True, (20, 20, 20), max(1, w // 600))
    return img


JACKET, TROUSERS, SKIN, HELMET = (40, 120, 230), (70, 60, 50), (150, 180, 220), (0, 200, 255)


def _draw_person(img: np.ndarray, keypoints: Sequence[Sequence[float]]) -> None:
    """Фигура рабочего по 17 точкам позы COCO — совпадает со скелетом от «нейросети»."""
    h, w = img.shape[:2]
    p = [(round(x * w), round(y * h)) for x, y, _ in keypoints]
    limb = max(4, round(abs(p[6][0] - p[5][0]) * 0.45))  # толщина конечностей ~ ширина плеч

    def line(a: int, b: int, color: tuple[int, int, int], k: float = 1.0) -> None:
        cv2.line(img, p[a], p[b], color, max(2, round(limb * k)), cv2.LINE_AA)

    for hip, knee, ankle in ((11, 13, 15), (12, 14, 16)):  # ноги
        line(hip, knee, TROUSERS)
        line(knee, ankle, TROUSERS)
    torso = np.array([p[5], p[6], p[12], p[11]], np.int32)
    cv2.fillPoly(img, [torso], JACKET)
    cv2.polylines(img, [torso], True, JACKET, limb // 2, cv2.LINE_AA)
    for shoulder, elbow, wrist in ((5, 7, 9), (6, 8, 10)):  # руки
        line(shoulder, elbow, JACKET, 0.8)
        line(elbow, wrist, SKIN, 0.6)
    head_r = max(4, round(abs(p[4][0] - p[3][0]) * 0.75))
    cv2.circle(img, p[0], head_r, SKIN, -1, cv2.LINE_AA)
    cv2.ellipse(img, (p[0][0], p[0][1] - head_r // 3), (head_r + 2, head_r), 0, 180, 360,
                HELMET, -1, cv2.LINE_AA)  # каска  # fmt: skip


def make_demo_video(
    path: Path = DEFAULT_PATH,
    size: tuple[int, int] = (1280, 720),
    fps: int = 25,
    zone: Sequence[Sequence[float]] = DEMO_ZONE,
) -> Path:
    w, h = size
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError(f"OpenCV не может записать видео в {path}")
    background = _background(w, h, zone)
    # Ложный кадр — ошибка «нейросети», на видео его нет.
    detector = MockDetector(zone, false_positive=False)
    try:
        for idx in range(int((DURATION_S + 1) * fps)):
            ts = idx / fps
            frame = background.copy()
            dets = detector.infer(Frame("demo", idx, ts, w, h))
            for det in dets.detections:
                _draw_person(frame, det.keypoints)
            cv2.putText(frame, f"CAM-03  {ts:6.2f}s", (16, h - 16), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (230, 230, 230), 2, cv2.LINE_AA)  # fmt: skip
            writer.write(frame)
    finally:
        writer.release()
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--size", default="1280x720", help="ШИРИНАxВЫСОТА")
    parser.add_argument("--fps", type=int, default=25)
    args = parser.parse_args()
    width, height = (int(v) for v in args.size.lower().split("x"))
    print(make_demo_video(args.path, (width, height), args.fps))
    print(f"Длительность {DURATION_S + 1:.0f} с; ложный кадр детектора на {FALSE_POSITIVE_TS} с.")
