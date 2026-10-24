"""Повторный экспорт YOLOv8s в ONNX под требования SDK LinQ (в ОТДЕЛЬНОМ окружении с ultralytics).

    python -m venv linq/.venv-export && linq/.venv-export/Scripts/pip install ultralytics
    linq/.venv-export/Scripts/python linq/export_onnx.py [--weights backend/models/yolov8s.pt]
        [--imgsz 640] [--opset 12] [--out linq/build]

Параметры по умолчанию дают тот же файл, что backend/models/yolov8s.onnx:
статичный вход 1×3×640×640, без NMS, simplify. Если SDK потребует другой opset или размер —
по tpu_framework.pdf. ultralytics и torch в окружение backend НЕ ставятся.
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--weights", type=Path, default=ROOT / "backend" / "models" / "yolov8s.pt")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--opset", type=int, default=12)
    parser.add_argument("--no-simplify", action="store_true")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "build")
    args = parser.parse_args()
    try:
        from ultralytics import YOLO
    except ImportError:
        print("Нужен ultralytics в отдельном окружении (см. docstring), не в backend.")
        return 1
    if not args.weights.is_file():
        print(f"Нет весов: {args.weights}")
        return 1
    model = YOLO(str(args.weights))
    path = Path(model.export(format="onnx", imgsz=args.imgsz, opset=args.opset,
                             simplify=not args.no_simplify, dynamic=False, nms=False))  # fmt: skip
    args.out.mkdir(parents=True, exist_ok=True)
    dest = args.out / f"{args.weights.stem}_{args.imgsz}_op{args.opset}.onnx"
    shutil.move(str(path), dest)
    print(f"-> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
