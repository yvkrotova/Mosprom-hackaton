"""Квантование и компиляция YOLOv8s в программу LinQ TPU — КАРКАС до получения SDK.

    python linq/compile.py [--onnx backend/models/yolov8s.onnx] [--calibration linq/calibration]

Что делает сейчас:
1. проверяет входы: ONNX-модель (статичные формы) и калибровочный набор (prepare_calibration.py);
2. пишет linq/build/manifest.json: хеш ONNX, параметры входа, калибровка, дата, версия SDK;
3. останавливается: команды квантования и компиляции берутся ТОЛЬКО из tpu_framework.pdf
   (tpu_framework_2026.9.45.zip), их здесь нет — они не угадываются.

Когда SDK будет: впишите вызовы в quantize_and_compile() по документации, программа TPU должна
лечь в linq/build/ (расширение .tpu — так в примере организаторов), заполните sdk_version.
Пошагово — docs/LINQ_TODO_FOR_HUMAN.md.
"""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def quantize_and_compile(onnx_path: Path, calibration: Path, out_dir: Path) -> Path:
    """TODO(LinQ SDK): квантование int8 по калибровочному набору и компиляция в .tpu."""
    raise NotImplementedError(
        "Команды SDK LinQ не известны: скачайте tpu_framework_2026.9.45.zip, прочитайте "
        "tpu_framework.pdf и впишите вызовы сюда (см. docs/LINQ_TODO_FOR_HUMAN.md, шаг 3)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--onnx", type=Path, default=ROOT / "backend" / "models" / "yolov8s.onnx")
    parser.add_argument("--calibration", type=Path, default=HERE / "calibration")
    parser.add_argument("--out", type=Path, default=HERE / "build")
    parser.add_argument(
        "--sdk-version", default=None, help="Версия SDK из tpu_framework (для manifest)"
    )
    args = parser.parse_args()

    if not args.onnx.is_file():
        print(f"Нет модели: {args.onnx}")
        return 1
    calib_meta = args.calibration / "calibration.json"
    if not calib_meta.is_file():
        print("Нет калибровочного набора: python linq/prepare_calibration.py")
        return 1
    calib = json.loads(calib_meta.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "onnx": {
            "path": str(args.onnx.relative_to(ROOT)).replace("\\", "/"),
            "sha256": sha256(args.onnx),
        },
        "input": calib["input"],
        "calibration": {
            "count": calib["count"],
            "path": str(args.calibration.relative_to(ROOT)).replace("\\", "/"),
        },
        "sdk_version": args.sdk_version,
        "program": None,
        "status": "ожидает SDK",
    }
    try:
        program = quantize_and_compile(args.onnx, args.calibration, args.out)
        manifest.update(program=program.name, program_sha256=sha256(program), status="собрано")
    except NotImplementedError as exc:
        manifest["status"] = f"не собрано: {exc}"
        print(exc)
    (args.out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"-> {args.out / 'manifest.json'} ({manifest['status'][:40]})")
    return 0 if manifest["program"] else 2


if __name__ == "__main__":
    sys.exit(main())
