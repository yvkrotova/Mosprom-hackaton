"""Качество после квантования: детекции LinQ против эталона ONNX CPU на тех же кадрах.

    backend/.venv/Scripts/python linq/compare_with_onnx.py --onnx dets_onnx.jsonl
        --linq dets_linq.jsonl [--label person] [--iou 0.5] [--name te1]

Оба файла — JSONL FrameDetections (tools/export_detections.py для ONNX, notebook
linq/notebooks/run_linq_detections.ipynb для LinQ). Кадры сопоставляются по frame_idx, боксы класса
label — жадно по IoU ≥ порога. Отчёт: доля совпавших, средний IoU, разница уверенности, пропуски
(есть в ONNX, нет в LinQ) и лишние (есть в LinQ, нет в ONNX) — в linq/reports/compare_<name>.json.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPORTS = Path(__file__).resolve().parent / "reports"


def load(path: Path) -> dict[int, dict]:
    """frame_idx -> запись с рамками в долях 0..1. Понимает v1 (доли) и v2 (заголовок, пиксели)."""
    out, size = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "header":
            size = (rec["video"]["width"], rec["video"]["height"])
            continue
        if rec.get("type"):
            continue  # сводка
        if size:
            w, h = size
            for det in rec["detections"]:
                x1, y1, x2, y2 = det["bbox"]
                det["bbox"] = [x1 / w, y1 / h, x2 / w, y2 / h]
        out[rec["frame_idx"]] = rec
    return out


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def compare(
    onnx: dict[int, dict], linq: dict[int, dict], label: str = "person", thr: float = 0.5
) -> dict:
    frames = sorted(set(onnx) & set(linq))
    ious, dconf, matched, missed, extra = [], [], 0, 0, 0
    total_ref = total_linq = 0
    for idx in frames:
        ref = [d for d in onnx[idx]["detections"] if d["label"] == label]
        got = [d for d in linq[idx]["detections"] if d["label"] == label]
        total_ref += len(ref)
        total_linq += len(got)
        pairs = sorted(
            (
                (iou(r["bbox"], g["bbox"]), i, j)
                for i, r in enumerate(ref)
                for j, g in enumerate(got)
            ),
            reverse=True,
        )
        used_r, used_g = set(), set()
        for score, i, j in pairs:
            if score < thr:
                break
            if i in used_r or j in used_g:
                continue
            used_r.add(i)
            used_g.add(j)
            ious.append(score)
            dconf.append(got[j]["confidence"] - ref[i]["confidence"])
        matched += len(used_r)
        missed += len(ref) - len(used_r)
        extra += len(got) - len(used_g)

    def ms(recs, key):
        vals = [r.get(key) for r in recs.values() if r.get(key) is not None]
        return round(float(np.mean(vals)), 2) if vals else None

    return {
        "frames_compared": len(frames),
        "frames_only_onnx": len(set(onnx) - set(linq)),
        "frames_only_linq": len(set(linq) - set(onnx)),
        "label": label,
        "iou_threshold": thr,
        "onnx_boxes": total_ref,
        "linq_boxes": total_linq,
        "matched": matched,
        "match_rate": round(matched / total_ref, 3) if total_ref else None,
        "mean_iou": round(float(np.mean(ious)), 3) if ious else None,
        "mean_conf_diff": round(float(np.mean(dconf)), 3) if dconf else None,
        "missed_by_linq": missed,
        "extra_in_linq": extra,
        "inference_ms_avg": {"onnx": ms(onnx, "inference_ms"), "linq": ms(linq, "inference_ms")},
        "detector_kind": {
            "onnx": next(iter(onnx.values()), {}).get("detector_kind"),
            "linq": next(iter(linq.values()), {}).get("detector_kind"),
        },  # fmt: skip
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--onnx", type=Path, required=True)
    parser.add_argument("--linq", type=Path, required=True)
    parser.add_argument("--label", default="person")
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--name", default="video")
    args = parser.parse_args()
    report = compare(load(args.onnx), load(args.linq), args.label, args.iou)
    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"compare_{args.name}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
