"""Калибровочный набор для квантования YOLOv8s под LinQ: кадры равномерно из тестовых роликов.

    backend/.venv/Scripts/python linq/prepare_calibration.py [--count 200] [--out linq/calibration]

Каждый кадр проходит ТОТ ЖЕ letterbox, что в конвейере (backend/.../detectors/yolo_common.py):
640×640, сохранение пропорций, поля 114, BGR→RGB, /255, NCHW. Иначе статистика активаций при
калибровке не совпадёт с реальной и точность после квантования просядет.

Формат, который ждёт SDK, мы пока не знаем (tpu_framework.pdf не скачан), поэтому пишем два:
- npy/<NNNN>.npy — float32 [1, 3, 640, 640], RGB, 0..1 — ровно вход ONNX «images»;
- png/<NNNN>.png — тот же кадр после letterbox, uint8 RGB 640×640 (если SDK сам нормализует).
calibration.json — откуда каждый кадр (ролик, video_ts) и параметры препроцессинга.
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.pipeline.detectors.yolo_common import letterbox  # noqa: E402
from tools.videos import select, video_path  # noqa: E402


def sample(entry: dict, count: int) -> list[tuple[float, np.ndarray]]:
    cap = cv2.VideoCapture(str(video_path(entry)))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    out = []
    for idx in np.linspace(0, max(total - 2, 0), count).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, img = cap.read()
        if ok:
            out.append((round(idx / fps, 3), img))
    cap.release()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--size", type=int, default=640)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "calibration")
    parser.add_argument("--ids", nargs="*")
    args = parser.parse_args()

    videos = select(args.ids)
    if not videos:
        print("Нет тестовых роликов (backend/data/videos/manifest.json)")
        return 1
    (args.out / "npy").mkdir(parents=True, exist_ok=True)
    (args.out / "png").mkdir(parents=True, exist_ok=True)
    per_video = [
        args.count // len(videos) + (i < args.count % len(videos)) for i in range(len(videos))
    ]
    items, n = [], 0
    for entry, k in zip(videos, per_video, strict=True):
        for ts, img in sample(entry, k):
            tensor, info = letterbox(img, args.size)
            np.save(args.out / "npy" / f"{n:04d}.npy", tensor)
            rgb_u8 = (tensor[0].transpose(1, 2, 0) * 255).round().astype(np.uint8)
            cv2.imencode(".png", cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2BGR))[1].tofile(
                str(args.out / "png" / f"{n:04d}.png")
            )
            items.append({"index": n, "video": entry["id"], "video_ts": ts,
                          "scale": info.scale, "pad": [info.pad_x, info.pad_y]})  # fmt: skip
            n += 1
        print(f"  {entry['id']}: {k} кадров")
    meta = {
        "count": n,
        "input": {
            "name": "images",
            "shape": [1, 3, args.size, args.size],
            "dtype": "float32",
            "layout": "NCHW",
            "channels": "RGB",
            "range": [0, 1],
            "letterbox_pad": 114,
        },  # fmt: skip
        "formats": {"npy": "float32 NCHW 0..1 (вход ONNX)", "png": "uint8 RGB после letterbox"},
        "items": items,
    }
    (args.out / "calibration.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Готово: {n} кадров -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
