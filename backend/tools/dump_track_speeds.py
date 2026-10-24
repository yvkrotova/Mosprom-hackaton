"""Скорость треков по кадрам — для калибровки порога сценария run_fast_movement.

    python tools/dump_track_speeds.py --video <path|id> [--detector onnx|linq|replay] [--fps 10]
                                      --out speeds.csv [--render speeds.mp4]
                                      [--params '{"min_track_age_s": 0.3}']

CSV: video_ts, track_id, speed_raw, speed_smooth, bbox_h (пиксели), unknown (почему неизвестна).
В конце печатается сводка по трекам: медиана и максимум сглаженной скорости, сколько кадров скорость
была известна. Скорость — «ростов человека в секунду» (смещение ног / высота рамки), не м/с.
Без --params используются параметры сценария по умолчанию.
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import median

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.scenarios.run_fast_movement import (  # noqa: E402
    RunFastMovementRule,
    RunFastParams,
)
from app.pipeline.tracker import TrackerConfig, build_tracker  # noqa: E402
from tools.videos import cached_detections, frames_with_images, manifest  # noqa: E402


def entry_for(video: str) -> dict:
    for v in manifest():
        if v["id"] == video or Path(v["file"]).name == Path(video).name:
            return v
    return {"id": Path(video).stem, "file": str(Path(video).resolve())}


def analyse(records, params: RunFastParams, tracker_kind: str = "bytetrack"):
    tracker = build_tracker(TrackerConfig(kind=tracker_kind))
    rule = RunFastMovementRule("calibration")
    rows, per_frame, hits = [], [], []
    for rec in records:
        tracks = [t for t in tracker.update(rec) if t.state == "confirmed"]
        hits.extend(
            (rec.video_ts, h.track_id) for h in rule.update(tracks, [], params, rec.video_ts)
        )
        info = rule.track_info()
        snap = []
        for t in tracks:
            st = rule._states.get(t.track_id)  # внутреннее состояние — только для отладки порога
            i = info.get(t.track_id, {})
            h_px = (t.bbox[3] - t.bbox[1]) * rec.frame_h
            rows.append({
                "video_ts": round(rec.video_ts, 3), "track_id": t.track_id,
                "speed_raw": None if st is None or st.raw is None else round(st.raw, 3),
                "speed_smooth": i.get("speed"), "bbox_h": round(h_px, 1),
                "unknown": i.get("speed_unknown") or "",
            })  # fmt: skip
            snap.append((t, i))
        per_frame.append(snap)
    return rows, per_frame, hits


def summarize(rows) -> dict[int, dict]:
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(r["track_id"], []).append(r)
    out = {}
    for tid, rs in by.items():
        sp = [r["speed_smooth"] for r in rs if r["speed_smooth"] is not None]
        out[tid] = {
            "frames": len(rs),
            "known": len(sp),
            "median": round(median(sp), 2) if sp else None,
            "max": round(max(sp), 2) if sp else None,
            "first_s": rs[0]["video_ts"],
            "last_s": rs[-1]["video_ts"],
            "median_h_px": round(median(r["bbox_h"] for r in rs), 1),
        }
    return out


def render(entry, fps, per_frame, path: Path, threshold: float) -> None:
    writer = None
    for frame, snap in zip(frames_with_images(entry, fps), per_frame, strict=False):
        img = frame.image.copy()
        h, w = img.shape[:2]
        if writer is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        scale = max(0.35, min(w, h) / 900)
        for t, i in snap:
            sp = i.get("speed")
            color = (40, 40, 230) if sp is not None and sp >= threshold else (80, 200, 80)
            x1, y1, x2, y2 = (
                round(t.bbox[0] * w),
                round(t.bbox[1] * h),
                round(t.bbox[2] * w),
                round(t.bbox[3] * h),
            )
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 1 if w < 400 else 2)
            text = f"#{t.track_id} {sp:.1f}" if sp is not None else f"#{t.track_id} ?"
            cv2.putText(
                img,
                text,
                (x1, max(10, y1 - 3)),
                cv2.FONT_HERSHEY_SIMPLEX,
                scale,
                color,
                1,
                cv2.LINE_AA,
            )
        writer.write(img)
    if writer is not None:
        writer.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--video", required=True, help="Путь к видео или id из manifest.json")
    parser.add_argument("--detector", default="onnx")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--tracker", default="bytetrack", choices=["bytetrack", "iou"])
    parser.add_argument("--params", default="{}", help="JSON с параметрами сценария")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--render", type=Path)
    args = parser.parse_args()

    entry = entry_for(args.video)
    params = RunFastParams.model_validate(json.loads(args.params))
    records = cached_detections(entry, args.detector, args.fps)
    rows, per_frame, hits = analyse(records, params, args.tracker)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["video_ts"])
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(rows)
    print(f"{entry['id']}: треков {len(summary)}, кадров с треками {len(per_frame)} -> {args.out}")
    for tid, s in summary.items():
        print(f"  #{tid}: {s}")
    episodes = sorted({tid for _, tid in hits})
    print(f"  треки с подтверждённым бегом (порог {params.speed_threshold}): {episodes}")
    if args.render:
        render(entry, args.fps, per_frame, args.render, params.speed_threshold)
        print(f"  рендер -> {args.render}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
