"""Инвентаризация тестовых видео: параметры, контактные листы, оценка числа людей.

    python tools/video_inventory.py [видео ...] [--out data/videos] [--detector onnx|none]

Без аргументов берёт ролики из data/videos/ и data/storage/videos/0_te*.mp4 (оригиналы не меняются).
Для каждого ролика:
- длительность, FPS, разрешение, кодек (FOURCC), поворот из метаданных и читается ли OpenCV;
- контактный лист: 12 равномерных кадров с подписью video_ts -> <out>/previews/<имя>.jpg;
- число людей на этих кадрах по OnnxDetector (если есть модель).
Результат — <out>/inventory.json (технические поля для manifest.json).
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BACKEND_DIR, Settings  # noqa: E402
from app.pipeline.interfaces import Frame  # noqa: E402

DEFAULT_GLOBS = ["data/videos/*.mp4", "data/videos/*.avi", "data/videos/*.mov", "data/videos/*.mkv",
                 "data/storage/videos/0_te*.mp4"]  # fmt: skip
SHEET_FRAMES = 12


def probe(path: Path) -> dict:
    cap = cv2.VideoCapture(str(path))
    info = {
        "file": str(path.relative_to(BACKEND_DIR)).replace("\\", "/"),
        "size_mb": round(path.stat().st_size / 1e6, 1),
        "readable": cap.isOpened(),
    }
    if not cap.isOpened():
        return info
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    ok, first = cap.read()
    info.update(
        fps=round(fps, 3),
        frames=frames,
        duration_s=round(frames / fps, 2) if fps else None,
        width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        codec="".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4)).strip("\x00"),
        # OpenCV 4.11 сам применяет поворот из метаданных (CAP_PROP_ORIENTATION_AUTO);
        # decoded_* — размер кадра, который реально получает конвейер.
        rotation_meta=int(cap.get(cv2.CAP_PROP_ORIENTATION_META)),
        decoded_width=first.shape[1] if ok else None,
        decoded_height=first.shape[0] if ok else None,
    )
    cap.release()
    return info


def sample_frames(path: Path, count: int) -> list[tuple[float, np.ndarray]]:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    out = []
    for idx in np.linspace(0, max(total - 2, 0), count).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, img = cap.read()
        if ok:
            out.append((idx / fps, img))
    cap.release()
    return out


def contact_sheet(
    frames: list[tuple[float, np.ndarray]], people: list[int | None], cols: int = 4
) -> np.ndarray:
    tiles = []
    for (ts, img), n in zip(frames, people, strict=True):
        h, w = img.shape[:2]
        tile = cv2.resize(img, (360, round(360 * h / w)))
        label = f"{ts:6.1f} s" + (f" | people {n}" if n is not None else "")
        cv2.rectangle(tile, (0, 0), (tile.shape[1], 26), (0, 0, 0), -1)
        cv2.putText(
            tile, label, (6, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA
        )
        tiles.append(tile)
    th = max(t.shape[0] for t in tiles)
    tiles = [cv2.copyMakeBorder(t, 0, th - t.shape[0], 0, 0, cv2.BORDER_CONSTANT) for t in tiles]
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i : i + cols]) for i in range(0, len(tiles), cols)]
    return np.vstack(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("videos", nargs="*", type=Path)
    parser.add_argument("--out", type=Path, default=BACKEND_DIR / "data" / "videos")
    parser.add_argument("--detector", choices=["onnx", "none"], default="onnx")
    args = parser.parse_args()

    videos = args.videos or sorted({p for g in DEFAULT_GLOBS for p in BACKEND_DIR.glob(g)})
    detector = None
    if args.detector == "onnx":
        from app.pipeline.detectors.onnx import OnnxDetector

        detector = OnnxDetector(Settings(_env_file=None, detector_conf_threshold=0.35))
    (args.out / "previews").mkdir(parents=True, exist_ok=True)

    report = []
    for path in videos:
        info = probe(path)
        print(f"{path.name}: {info}")
        if info.get("readable"):
            frames = sample_frames(path, SHEET_FRAMES)
            people = []
            for i, (ts, img) in enumerate(frames):
                if detector is None:
                    people.append(None)
                    continue
                dets = detector.infer(Frame("inv", i, ts, img.shape[1], img.shape[0], img))
                people.append(sum(d.label == "person" for d in dets.detections))
            sheet = contact_sheet(frames, people)
            preview = args.out / "previews" / (path.stem[:60] + ".jpg")
            cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tofile(str(preview))
            info["preview"] = str(preview.relative_to(BACKEND_DIR)).replace("\\", "/")
            info["people_per_sampled_frame"] = people
        report.append(info)
    (args.out / "inventory.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"-> {args.out / 'inventory.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
