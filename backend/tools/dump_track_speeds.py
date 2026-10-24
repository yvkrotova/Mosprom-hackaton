"""Скорость треков по кадрам — для калибровки порога сценария run_fast_movement.

    python tools/dump_track_speeds.py --video <path|id> [--detector onnx|linq|replay] [--fps 10]
                                      --out speeds.csv [--render speeds.mp4]
                                      [--params '{"min_track_age_s": 0.3}']

CSV: video_ts, track_id, speed (роста/с или пусто), level (violation | hold | unknown | ''),
bbox_h (пиксели), unknown (почему скорость неизвестна). Скорость — kinematics.estimate_speed,
уровень — то, что правило отдаёт EpisodeManager на этом кадре. В конце — сводка по трекам и эпизоды,
которые собрал бы EpisodeManager с этими параметрами.
"""

import argparse
import csv
import json
import sys
import uuid
from pathlib import Path
from statistics import median

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.base import utcnow  # noqa: E402
from app.pipeline.episodes import EpisodeManager, RuleContext  # noqa: E402
from app.pipeline.interfaces import FrameContext, HitLevel  # noqa: E402
from app.pipeline.scenarios.run_fast_movement import (  # noqa: E402
    RunFastMovementRule,
    RunFastParams,
    track_speed,
)
from app.pipeline.tracker import TrackerConfig, build_tracker  # noqa: E402
from app.schemas.enums import Priority  # noqa: E402
from tools.videos import cached_detections, frames_of, frames_with_images, manifest  # noqa: E402


class _Episodes:
    def __init__(self) -> None:
        self.final: dict[str, dict] = {}

    def send_event(self, event_id, payload):
        self.final[event_id] = payload

    def send_evidence(self, *_args):
        pass


def entry_for(video: str) -> dict:
    for v in manifest():
        if v["id"] == video or Path(v["file"]).name == Path(video).name:
            return v
    return {"id": Path(video).stem, "file": str(Path(video).resolve())}


def analyse(records, params: RunFastParams, tracker_kind: str = "bytetrack"):
    tracker = build_tracker(TrackerConfig(kind=tracker_kind))
    rule_id, source_id = str(uuid.uuid4()), str(uuid.uuid4())
    rule = RunFastMovementRule(rule_id)
    sink = _Episodes()
    context = RuleContext(rule_id, rule.scenario_type, rule.title, Priority.HIGH, 1, params)
    episodes = EpisodeManager(
        source_id, "calibration", {rule_id: context}, {}, sink, "cache", utcnow()
    )
    rows, per_frame = [], []
    for rec, frame in zip(records, frames_of(records), strict=True):
        tracks = tracker.update(rec)
        confirmed = [t for t in tracks if t.state == "confirmed"]
        ctx = FrameContext(frame, rec.video_ts, confirmed, tracks, rec)
        hits = rule.update(ctx, [], params)
        episodes.process(frame, hits, confirmed, tracks)
        levels = {h.track_id: h.level for h in hits}
        snap = []
        for t in confirmed:
            est = track_speed(t, rec.video_ts, params)
            level = levels.get(t.track_id)
            rows.append({
                "video_ts": round(rec.video_ts, 3), "track_id": t.track_id,
                "speed": None if est.speed is None else round(est.speed, 3),
                "level": level.value if level else "",
                "bbox_h": round((t.bbox[3] - t.bbox[1]) * rec.frame_h, 1),
                "unknown": est.reason or "",
            })  # fmt: skip
            snap.append((t, est.speed, level))
        per_frame.append(snap)
    episodes.finish()
    return rows, per_frame, list(sink.final.values())


def summarize(rows) -> dict[int, dict]:
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(r["track_id"], []).append(r)
    out = {}
    for tid, rs in by.items():
        sp = [r["speed"] for r in rs if r["speed"] is not None]
        out[tid] = {
            "frames": len(rs),
            "known": len(sp),
            "median": round(median(sp), 2) if sp else None,
            "max": round(max(sp), 2) if sp else None,
            "first_s": rs[0]["video_ts"],
            "last_s": rs[-1]["video_ts"],
            "median_h_px": round(float(median(r["bbox_h"] for r in rs)), 1),
        }
    return out


def render(entry, fps, per_frame, path: Path) -> None:
    colors = {HitLevel.VIOLATION: (40, 40, 230), HitLevel.HOLD: (0, 160, 255)}
    writer = None
    for frame, snap in zip(frames_with_images(entry, fps), per_frame, strict=False):
        img = frame.image.copy()
        h, w = img.shape[:2]
        if writer is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        scale = max(0.35, min(w, h) / 900)
        for t, speed, level in snap:
            color = colors.get(level, (80, 200, 80))
            x1, y1 = round(t.bbox[0] * w), round(t.bbox[1] * h)
            x2, y2 = round(t.bbox[2] * w), round(t.bbox[3] * h)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 1 if w < 400 else 2)
            text = f"#{t.track_id} {speed:.1f}" if speed is not None else f"#{t.track_id} ?"
            cv2.putText(img, text, (x1, max(10, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1)
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
    rows, per_frame, episodes = analyse(records, params, args.tracker)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["video_ts"])
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(rows)
    print(f"{entry['id']}: треков {len(summary)}, кадров {len(per_frame)} -> {args.out}")
    for tid, s in summary.items():
        print(f"  #{tid}: {s}")
    found = [(e["track_id"], e["video_ts_start"], e["video_ts_end"]) for e in episodes]
    print(f"  эпизоды бега (порог {params.speed_threshold}): {found}")
    if args.render:
        render(entry, args.fps, per_frame, args.render)
        print(f"  рендер -> {args.render}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
