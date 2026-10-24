"""Проверка трекеров на тестовых видео: IoU против ByteTrack, рендер треков.

    python tools/track_videos.py [--ids te1 te21] [--fps 10] [--render]
                                 [--out data/videos/track_stats.json]

Для каждого ролика и трекера: число уникальных подтверждённых ID, средняя длительность трека,
«обрывы» — новый трек начался рядом (≤ 1 роста) с местом, где другой трек пропал меньше 1.5 с назад
(без разметки истины это лучшая оценка переключений и обрывов ID).
--render: MP4 с треками ByteTrack в data/videos/rendered/<id>_tracks.mp4 и 4 скриншота рядом.
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.tracker import TrackerConfig, build_tracker  # noqa: E402
from tools.videos import VIDEOS_DIR, cached_detections, frames_with_images, select  # noqa: E402

COLORS = [(248, 189, 56), (153, 211, 52), (250, 139, 167), (182, 114, 244), (238, 211, 34),
          (53, 230, 163), (60, 146, 251), (252, 132, 192)]  # fmt: skip


def run_tracker(records, kind: str) -> tuple[dict[int, dict], list[list]]:
    """Треки: {id: {first, last, frames, start_foot, end_foot, h}} и снимки кадров для рендера."""
    tracker = build_tracker(TrackerConfig(kind=kind))
    tracks: dict[int, dict] = {}
    per_frame = []
    for rec in records:
        snap = []
        for t in tracker.update(rec):
            if t.state != "confirmed":
                if t.state == "lost":
                    snap.append((t.track_id, t.bbox, "lost"))
                continue
            x1, y1, x2, y2 = t.bbox
            foot = ((x1 + x2) / 2 * rec.frame_w, y2 * rec.frame_h)
            h = (y2 - y1) * rec.frame_h
            info = tracks.setdefault(
                t.track_id, {"first": rec.video_ts, "frames": 0, "start_foot": foot}
            )
            info.update(last=rec.video_ts, end_foot=foot, h=h)
            info["frames"] += 1
            snap.append((t.track_id, t.bbox, "confirmed"))
        per_frame.append(snap)
    return tracks, per_frame


def fragments(tracks: dict[int, dict]) -> int:
    """Новые треки, начавшиеся рядом с только что пропавшим (вероятный обрыв/переключение ID)."""
    count = 0
    for tid, t in tracks.items():
        for oid, o in tracks.items():
            if oid == tid or not 0 < t["first"] - o["last"] <= 1.5:
                continue
            d = np.hypot(
                t["start_foot"][0] - o["end_foot"][0], t["start_foot"][1] - o["end_foot"][1]
            )
            if d <= o["h"]:
                count += 1
                break
    return count


def summary(tracks: dict[int, dict]) -> dict:
    durations = [t["last"] - t["first"] for t in tracks.values()]
    return {
        "unique_ids": len(tracks),
        "mean_track_s": round(float(np.mean(durations)), 2) if durations else 0.0,
        "long_tracks_ge_2s": sum(d >= 2 for d in durations),
        "fragments": fragments(tracks),
    }


def render(entry: dict, fps: float, per_frame: list[list], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{entry['id']}_tracks.mp4"
    writer = None
    shots = []
    frames = list(frames_with_images(entry, fps))
    marks = {int(len(frames) * k) for k in (0.15, 0.4, 0.65, 0.9)}
    for i, (frame, snap) in enumerate(zip(frames, per_frame, strict=False)):
        img = frame.image.copy()
        h, w = img.shape[:2]
        if writer is None:
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        thick = max(1, round(min(w, h) / 360))
        for tid, (x1, y1, x2, y2), state in snap:
            color = COLORS[tid % len(COLORS)]
            p1, p2 = (round(x1 * w), round(y1 * h)), (round(x2 * w), round(y2 * h))
            if state == "lost":
                cv2.rectangle(img, p1, p2, (120, 120, 120), 1)
                continue
            cv2.rectangle(img, p1, p2, color, thick)
            cv2.putText(img, f"#{tid}", (p1[0], max(12, p1[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                        max(0.4, min(w, h) / 900), color, thick, cv2.LINE_AA)  # fmt: skip
        cv2.putText(img, f"{frame.video_ts:5.1f}s ByteTrack", (8, 22), cv2.FONT_HERSHEY_SIMPLEX,
                    max(0.4, min(w, h) / 900), (255, 255, 255), thick, cv2.LINE_AA)  # fmt: skip
        writer.write(img)
        if i in marks:
            shot = out_dir / f"{entry['id']}_tracks_{frame.video_ts:05.1f}s.jpg"
            cv2.imencode(".jpg", img)[1].tofile(str(shot))
            shots.append(shot.name)
    if writer is not None:
        writer.release()
    return [path.name, *shots]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--detector", default="onnx")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--out", type=Path, default=VIDEOS_DIR / "track_stats.json")
    args = parser.parse_args()

    report = {}
    for entry in select(args.ids):
        records = cached_detections(entry, args.detector, args.fps)
        row = {}
        for kind in ("iou", "bytetrack"):
            tracks, per_frame = run_tracker(records, kind)
            row[kind] = summary(tracks)
            if kind == "bytetrack" and args.render:
                row["rendered"] = render(entry, args.fps, per_frame, VIDEOS_DIR / "rendered")
        report[entry["id"]] = row
        print(f"{entry['id']:12} iou={row['iou']}  bytetrack={row['bytetrack']}")
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
