"""Run a YOLO detector with ByteTrack tracking over a camera video / stream.

Settings come from configs/cameras.yaml (`defaults`, then the camera entry);
command-line arguments override them.

Outputs (in --out), <name> = camera_id (or the source file name without --camera):
  <name>_annotated.mp4   - video with boxes and track ids
  <name>_detections.csv  - one row per detection
  <name>_benchmark.json  - per-frame timings, FPS and device info

Example:
  python scripts/run_yolo_video.py --camera cam_01
  python scripts/run_yolo_video.py --camera cam_01 --classes 0,2 --conf 0.25 --max-frames 300
  python scripts/run_yolo_video.py --source data/videos/0_te1.mp4 --classes 0
"""

import argparse
import json
import platform
import time
from collections import Counter
from pathlib import Path

import cv2
import pandas as pd
import torch
import yaml
from ultralytics import YOLO

DEFAULTS = {
    "model": str(Path("models") / "yolov8s.pt"),
    "classes": [0],
    "conf": 0.35,
    "imgsz": 640,
    "device": "auto",
    "tracker": "bytetrack.yaml",
    "max_frames": 0,
}
OVERRIDABLE = ("model", "source", "classes", "conf", "imgsz", "device", "tracker", "max_frames")


def parse_classes(value):
    try:
        return [int(c) for c in value.split(",") if c.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers, got {value!r}")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(Path("configs") / "cameras.yaml"), help="cameras config (yaml)")
    p.add_argument("--camera", help="camera_id from the config (default: first camera if --source is not set)")
    # default None = not given on the command line, value comes from the config
    p.add_argument("--model", help="path to .pt/.onnx weights")
    p.add_argument("--source", help="path to input video or rtsp:// url")
    p.add_argument("--classes", type=parse_classes, help="comma-separated COCO class ids, e.g. 0 or 0,2,7")
    p.add_argument("--conf", type=float, help="confidence threshold")
    p.add_argument("--imgsz", type=int, help="inference image size")
    p.add_argument("--device", help="auto | cpu | 0 | cuda:0 | mps")
    p.add_argument("--out", default="outputs", help="output directory")
    p.add_argument("--tracker", help="ultralytics tracker config")
    p.add_argument("--max-frames", type=int, help="stop after N frames (0 = whole video)")
    return p.parse_args()


def load_settings(args):
    """Merge DEFAULTS < config `defaults` < camera entry < command-line args."""
    settings = dict(DEFAULTS, camera_id=None, zone=None, source=None)
    config_path = Path(args.config)
    camera = None
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        settings.update(config.get("defaults") or {})
        cameras = config.get("cameras") or []
        if args.camera:
            camera = next((c for c in cameras if c.get("camera_id") == args.camera), None)
            if camera is None:
                known = ", ".join(str(c.get("camera_id")) for c in cameras)
                raise SystemExit(f"camera {args.camera!r} not found in {config_path} (known: {known})")
        elif not args.source and cameras:
            camera = cameras[0]
    elif args.camera:
        raise SystemExit(f"config not found: {config_path}")
    if camera:
        settings.update(camera)

    for key in OVERRIDABLE:
        value = getattr(args, key)
        if value is not None:
            settings[key] = value
    if not settings["source"]:
        raise SystemExit("no source: pass --source or --camera (or add a camera to the config)")
    if isinstance(settings["classes"], int):
        settings["classes"] = [settings["classes"]]
    return settings


def is_stream(source):
    return "://" in str(source)


def resolve_device(device):
    if device != "auto":
        return device
    if torch.cuda.is_available():
        return "0"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def cpu_name():
    name = platform.processor()
    if platform.system() == "Windows":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    elif platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    name = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    return name


def device_info(device):
    info = {
        "device": device,
        "os": platform.platform(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "cpu": cpu_name(),
        "cpu_threads": torch.get_num_threads(),
        "gpu": None,
    }
    if device not in ("cpu", "mps") and torch.cuda.is_available():
        info["gpu"] = torch.cuda.get_device_name(0)
    elif device == "mps":
        info["gpu"] = "Apple MPS"
    return info


def main():
    args = parse_args()
    cfg = load_settings(args)
    device = resolve_device(str(cfg["device"]))
    source = cfg["source"] if is_stream(cfg["source"]) else Path(cfg["source"])
    if isinstance(source, Path) and not source.exists():
        raise SystemExit(f"source not found: {source}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = cfg["camera_id"] or (source.stem if isinstance(source, Path) else "stream")
    print(f"camera: {cfg['camera_id']} | zone: {cfg['zone']} | source: {source}")
    print(f"model: {cfg['model']} | classes: {cfg['classes']} | conf: {cfg['conf']} | "
          f"imgsz: {cfg['imgsz']} | device: {device}")

    cap = cv2.VideoCapture(str(source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    model = YOLO(str(cfg["model"]), task="detect")
    names = model.names

    video_path = out_dir / f"{stem}_annotated.mp4"
    writer = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))

    rows = []
    speeds = []
    n_frames = 0
    t_start = time.perf_counter()
    results = model.track(
        source=str(source),
        conf=cfg["conf"],
        imgsz=cfg["imgsz"],
        classes=cfg["classes"],
        device=device,
        tracker=cfg["tracker"],
        persist=True,
        stream=True,
        verbose=False,
    )
    for frame_idx, r in enumerate(results):
        speeds.append(r.speed)
        boxes = r.boxes
        if boxes is not None and len(boxes):
            ids = boxes.id.int().tolist() if boxes.id is not None else [None] * len(boxes)
            for tid, cls, conf, xyxy in zip(ids, boxes.cls.int().tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()):
                rows.append({
                    "frame": frame_idx,
                    "timestamp_sec": round(frame_idx / fps, 3),
                    "track_id": tid,
                    "class_id": cls,
                    "class_name": names[cls],
                    "conf": round(conf, 4),
                    "x1": round(xyxy[0], 1),
                    "y1": round(xyxy[1], 1),
                    "x2": round(xyxy[2], 1),
                    "y2": round(xyxy[3], 1),
                })
        writer.write(r.plot())
        n_frames += 1
        if cfg["max_frames"] and n_frames >= cfg["max_frames"]:
            break
    total_time = time.perf_counter() - t_start
    writer.release()

    columns = ["frame", "timestamp_sec", "track_id", "class_id", "class_name", "conf", "x1", "y1", "x2", "y2"]
    df = pd.DataFrame(rows, columns=columns)
    df["track_id"] = df["track_id"].astype("Int64")
    csv_path = out_dir / f"{stem}_detections.csv"
    df.to_csv(csv_path, index=False)

    def mean_ms(key):
        return round(sum(s[key] for s in speeds) / len(speeds), 2) if speeds else None

    benchmark = {
        "camera_id": cfg["camera_id"],
        "zone": cfg["zone"],
        "model": str(cfg["model"]),
        "source": str(source),
        "video": {"width": width, "height": height, "fps": round(fps, 2), "frames_total": total_frames},
        "params": {"classes": cfg["classes"], "conf": cfg["conf"], "imgsz": cfg["imgsz"], "tracker": cfg["tracker"]},
        "frames_processed": n_frames,
        "preprocess_ms": mean_ms("preprocess"),
        "inference_ms": mean_ms("inference"),
        "postprocess_ms": mean_ms("postprocess"),
        "total_time_sec": round(total_time, 2),
        # end-to-end FPS incl. decoding, tracking, drawing and video writing
        "fps_end_to_end": round(n_frames / total_time, 2) if total_time else None,
        # FPS of model only (pre + inference + post)
        "fps_model": round(1000 / (mean_ms("preprocess") + mean_ms("inference") + mean_ms("postprocess")), 2)
        if speeds else None,
        "device_info": device_info(device),
    }
    bench_path = out_dir / f"{stem}_benchmark.json"
    bench_path.write_text(json.dumps(benchmark, indent=2, ensure_ascii=False), encoding="utf-8")

    class_counts = Counter(df["class_name"])
    person_ids = df.loc[df["class_name"] == "person", "track_id"].dropna().nunique()

    print("\n=== Benchmark ===")
    print(f"device: {device} | cpu: {benchmark['device_info']['cpu']} | gpu: {benchmark['device_info']['gpu']}")
    print(f"preprocess {benchmark['preprocess_ms']} ms | inference {benchmark['inference_ms']} ms | "
          f"postprocess {benchmark['postprocess_ms']} ms")
    print(f"FPS model: {benchmark['fps_model']} | FPS end-to-end: {benchmark['fps_end_to_end']} | "
          f"total: {benchmark['total_time_sec']} s")
    print("\n=== Summary ===")
    print(f"frames processed: {n_frames} / {total_frames}")
    print(f"detections: {len(df)}")
    for name, cnt in class_counts.most_common():
        print(f"  {name:<16} {cnt}")
    print(f"unique person track_ids: {person_ids}")
    print(f"\nannotated video: {video_path}\ndetections csv:  {csv_path}\nbenchmark json:  {bench_path}")


if __name__ == "__main__":
    main()
