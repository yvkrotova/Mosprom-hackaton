"""Качество по эпизодам: весь конвейер (детектор → трекер → правила → эпизоды) против разметки.

    python tools/evaluate_episodes.py [--labels data/videos/labels.json]
                                      [--detector onnx|linq|replay] [--fps 10]
                                      [--tracker bytetrack|iou] [--out data/videos/evaluation.json]

Зоны — из manifest.json, правила — как их создаёт интерфейс и tools/seed.py --videos
(сценарии зоны + правила по умолчанию, дефолтные параметры). Совпадение —
пересечение интервалов эпизода и разметки при одном scenario_type. Отдельно для каждого сценария:
TP, FP, FN, precision, recall, оповещений на эпизод (event.created на найденный эпизод разметки,
должно быть 1), задержка от start_s разметки до подтверждения (video_ts_detected).
Детекции берутся из кэша data/videos/cache (см. tools/videos.py); кадры декодируются,
только когда есть сценарий с анализом сцены.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.runner import Pipeline  # noqa: E402
from app.pipeline.scenarios import list_scenarios  # noqa: E402
from app.pipeline.tracker import TrackerConfig  # noqa: E402
from app.schemas.internal import SourceConfig  # noqa: E402
from tools.videos import (  # noqa: E402
    LABELS,
    VIDEOS_DIR,
    CachedDetector,
    cached_detections,
    frames_of,
    frames_with_images,
    select,
)

T_ISO = "2026-10-24T00:00:00Z"
SCENARIOS = tuple(str(c.scenario_type) for c in list_scenarios())
SCENE_SCENARIOS = {str(c.scenario_type) for c in list_scenarios() if c.requires_scene_analysis}


class Sink:
    def __init__(self) -> None:
        self.final: dict[str, dict] = {}
        self.first_sends: list[str] = []

    def send_event(self, event_id, payload):
        if event_id not in self.final:
            self.first_sends.append(event_id)
        self.final[event_id] = payload

    def send_evidence(self, *_args):
        pass


def source_config(entry: dict, fps: float) -> SourceConfig:
    sid = str(uuid.uuid4())
    zones = [
        {
            "id": str(uuid.uuid4()),
            "source_id": sid,
            "name": z["name"],
            "polygon": z["polygon"],
            "created_at": T_ISO,
            "updated_at": T_ISO,
        }
        for z in entry.get("zones", [])
    ]
    # Правила — как их создаёт интерфейс: сценарии зоны (zones[].scenario_types, по умолчанию
    # danger_zone) и правила по умолчанию для сценариев с auto_enable — с дефолтными параметрами.
    targets: list[tuple[str, str | None]] = []
    for z, zone in zip(entry.get("zones", []), zones, strict=True):
        for scenario in z.get("scenario_types", ["danger_zone"]):
            targets.append((scenario, zone["id"]))
    for cls in list_scenarios():
        if cls.auto_enable and not cls.requires_zone:
            targets.append((str(cls.scenario_type), None))
    rules = [
        {
            "id": str(uuid.uuid4()),
            "source_id": sid,
            "zone_id": zone_id,
            "scenario_type": scenario,
            "enabled": True,
            "priority": "high",
            "params": {},
            "version": 1,
            "created_at": T_ISO,
            "updated_at": T_ISO,
        }
        for scenario, zone_id in targets
    ]
    return SourceConfig.model_validate(
        {
            "source": {
                "id": sid,
                "name": entry["title"],
                "kind": "file",
                "uri": entry["file"],
                "site": None,
                "status": "running",
                "last_error": None,
                "last_heartbeat_at": None,
                "analysis_fps": fps,
                "created_at": T_ISO,
                "updated_at": T_ISO,
            },
            "zones": zones,
            "rules": rules,
            "settings": {"episode_update_interval_s": 1.0, "heartbeat_interval_s": 2},
        }
    )


def overlaps(a0, a1, b0, b1) -> bool:
    return a0 <= b1 and b0 <= a1


def compare(found: list[dict], truth: list[dict], first_sends: list[str]) -> dict:
    matched_truth, matched_found = set(), set()
    delays, alerts = [], []
    for i, t in enumerate(truth):
        hits = [
            j
            for j, f in enumerate(found)
            if overlaps(f["video_ts_start"], f["video_ts_end"], t["start_s"], t["end_s"])
        ]
        if hits:
            matched_truth.add(i)
            matched_found.update(hits)
            first = min(hits, key=lambda j: found[j]["video_ts_detected"])
            delays.append(round(found[first]["video_ts_detected"] - t["start_s"], 2))
            alerts.append(sum(1 for j in hits if found[j]["_event_id"] in first_sends))
    tp, fn, fp = (
        len(matched_truth),
        len(truth) - len(matched_truth),
        len(found) - len(matched_found),
    )
    return {
        "truth": len(truth),
        "found": len(found),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / (tp + fp), 2) if tp + fp else None,
        "recall": round(tp / (tp + fn), 2) if tp + fn else None,
        "alerts_per_episode": round(sum(alerts) / len(alerts), 2) if alerts else None,
        "detection_delay_s": delays,
    }


def frames(entry: dict, records, cfg: SourceConfig, fps: float):
    """Картинки декодируются, только если есть правило с анализом сцены (blocked_exit)."""
    if any(r.scenario_type in SCENE_SCENARIOS for r in cfg.rules):
        return frames_with_images(entry, fps)
    return frames_of(records, cfg.source.id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--detector", default="onnx")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--tracker", default="bytetrack", choices=["bytetrack", "iou"])
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--out", type=Path, default=VIDEOS_DIR / "evaluation.json")
    args = parser.parse_args()

    all_labels = json.loads(args.labels.read_text(encoding="utf-8"))["videos"]
    report: dict = {
        "detector": args.detector,
        "fps": args.fps,
        "tracker": args.tracker,
        "videos": {},
    }
    totals = {s: [] for s in SCENARIOS}
    for entry in select(args.ids):
        records = cached_detections(entry, args.detector, args.fps)
        cfg = source_config(entry, args.fps)
        sink = Sink()
        Pipeline(
            cfg, CachedDetector(records), sink, tracker_config=TrackerConfig(kind=args.tracker)
        ).run(frames(entry, records, cfg, args.fps))
        found_all = [{**p, "_event_id": eid} for eid, p in sink.final.items()]
        truth_all = all_labels.get(entry["id"], [])
        row = {"draft_labels": any(t.get("draft") for t in truth_all)}
        for scenario in SCENARIOS:
            found = [f for f in found_all if f["scenario_type"] == scenario]
            truth = [t for t in truth_all if t["scenario_type"] == scenario]
            row[scenario] = compare(found, truth, sink.first_sends)
            row[scenario]["episodes"] = [
                (f["video_ts_start"], f["video_ts_detected"], f["video_ts_end"], f.get("details"))
                for f in found
            ]
            totals[scenario].append(row[scenario])
        report["videos"][entry["id"]] = row
        print(
            f"{entry['id']:12} "
            + "  ".join(
                f"{s}: TP {row[s]['tp']} FP {row[s]['fp']} FN {row[s]['fn']}" for s in SCENARIOS
            )
        )
    report["total"] = {
        s: {k: sum(r[k] for r in rows) for k in ("truth", "found", "tp", "fp", "fn")}
        for s, rows in totals.items()
    }
    for s, t in report["total"].items():
        t["precision"] = round(t["tp"] / (t["tp"] + t["fp"]), 2) if t["tp"] + t["fp"] else None
        t["recall"] = round(t["tp"] / (t["tp"] + t["fn"]), 2) if t["tp"] + t["fn"] else None
        print(f"ИТОГО {s}: {t}")
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
