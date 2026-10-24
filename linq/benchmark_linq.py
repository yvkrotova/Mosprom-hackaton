"""Замеры скорости LinQ TPU по JSONL, записанному notebook'ом в Jupyter (или LinqDetector локально).

    backend/.venv/Scripts/python linq/benchmark_linq.py dets_linq_te1.jsonl [...] [--warmup 5]
                                                         [--out linq/reports/benchmark_linq.json]

Из каждой строки FrameDetections берутся:
- inference_ms — полное время вызова TPU через API (включая сеть/передачу, если API удалённый);
- device_ms — время на самом TPU, если API его отдаёт (тогда сеть = inference_ms − device_ms);
- preprocess_ms / postprocess_ms — letterbox и декодирование + NMS на CPU.
Первые --warmup кадров каждого файла отбрасываются (прогрев). FPS — 1000 / сумма средних этапов.
Сравнение с ONNX CPU — tools/benchmark_detector.py на тех же роликах; таблица — docs/BENCHMARKS.md.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPORTS = Path(__file__).resolve().parent / "reports"


def stats(values: list[float]) -> dict | None:
    if not values:
        return None
    a = np.array(values)
    return {"avg": round(float(a.mean()), 2), "p50": round(float(np.percentile(a, 50)), 2),
            "p95": round(float(np.percentile(a, 95)), 2), "n": len(values)}  # fmt: skip


def summarize(path: Path, warmup: int) -> dict:
    recs = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ][warmup:]
    get = lambda key: [r[key] for r in recs if r.get(key) is not None]  # noqa: E731
    inference, device = get("inference_ms"), get("device_ms")
    network = [r["inference_ms"] - r["device_ms"] for r in recs if r.get("device_ms") is not None]
    out = {
        "file": path.name,
        "detector_kind": recs[0].get("detector_kind") if recs else None,
        "model_version": recs[0].get("model_version") if recs else None,
        "frame_size": [recs[0]["frame_w"], recs[0]["frame_h"]] if recs else None,
        "inference_ms": stats(inference),
        "device_ms": stats(device),
        "network_ms": stats(network),
        "preprocess_ms": stats(get("preprocess_ms")),
        "postprocess_ms": stats(get("postprocess_ms")),
    }
    total = sum(
        (out[k] or {}).get("avg", 0) for k in ("inference_ms", "preprocess_ms", "postprocess_ms")
    )
    out["fps_estimate"] = round(1000 / total, 2) if total else None
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--out", type=Path, default=REPORTS / "benchmark_linq.json")
    args = parser.parse_args()
    report = [summarize(p, args.warmup) for p in args.files]
    for r in report:
        if r["detector_kind"] and not str(r["detector_kind"]).startswith("linq"):
            print(f"ВНИМАНИЕ: {r['file']} посчитан детектором {r['detector_kind']}, это не LinQ")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
