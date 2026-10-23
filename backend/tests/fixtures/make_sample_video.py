"""Генерирует короткое синтетическое видео для тестов и демо (в репозиторий не коммитится).

python tests/fixtures/make_sample_video.py [путь] [--seconds 30] [--fps 10]
"""

import argparse
from pathlib import Path

import cv2
import numpy as np

DEFAULT_PATH = Path(__file__).with_name("sample.mp4")


def make_sample_video(
    path: Path = DEFAULT_PATH,
    seconds: float = 30.0,
    fps: int = 10,
    size: tuple[int, int] = (320, 240),
) -> Path:
    width, height = size
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"OpenCV не может записать видео в {path}")
    try:
        for idx in range(int(seconds * fps)):
            t = idx / fps
            frame = np.full((height, width, 3), 60, dtype=np.uint8)
            # «Пол цеха» и движущаяся фигура — чтобы кадры-доказательства не были пустыми.
            cv2.rectangle(frame, (0, height // 2), (width, height), (90, 90, 90), -1)
            x = int((t * 0.05 % 1.0) * (width - 30))
            cv2.rectangle(frame, (x, height // 2 - 20), (x + 30, height - 20), (40, 160, 230), -1)
            cv2.putText(
                frame, f"t={t:5.1f}s", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1
            )
            writer.write(frame)
    finally:
        writer.release()
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--fps", type=int, default=10)
    args = parser.parse_args()
    print(make_sample_video(args.path, args.seconds, args.fps))
