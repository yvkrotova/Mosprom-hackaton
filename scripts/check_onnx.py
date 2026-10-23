"""Compare an exported YOLOv8 ONNX model (run via plain onnxruntime) with the PyTorch model.

Two checks on a few frames of a video:
  1. raw output: same 640x640 input tensor -> max abs diff of the (1, 84, 8400) head output;
  2. final detections: onnxruntime + own letterbox/NMS vs ultralytics .pt predict,
     greedily matched by class and IoU.

Example:
  python scripts/check_onnx.py --onnx models/yolov8s.onnx --pt models/yolov8s.pt \
      --source data/videos/0_te1.mp4 --frames 0 60 120 180 240 300 360
"""

import argparse
import json
import time

import cv2
import numpy as np
import onnx
import onnxruntime as ort
import torch
from ultralytics import YOLO


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--onnx", default="models/yolov8s.onnx")
    p.add_argument("--pt", default="models/yolov8s.pt")
    p.add_argument("--source", required=True)
    p.add_argument("--frames", type=int, nargs="+", default=[0, 60, 120, 180, 240, 300, 360])
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--iou", type=float, default=0.7)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--out", default=None, help="optional path to save report json")
    return p.parse_args()


def letterbox(img, size):
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nh, nw = round(h * r), round(w * r)
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return canvas, r, left, top


def preprocess(img, size):
    lb, r, dx, dy = letterbox(img, size)
    x = lb[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    return np.ascontiguousarray(x), r, dx, dy


def postprocess(out, r, dx, dy, conf_thr, iou_thr):
    pred = out[0].T  # (8400, 84): cx, cy, w, h, 80 class scores
    scores = pred[:, 4:]
    cls = scores.argmax(1)
    conf = scores[np.arange(len(cls)), cls]
    keep = conf >= conf_thr
    pred, cls, conf = pred[keep], cls[keep], conf[keep]
    if not len(pred):
        return np.zeros((0, 6), dtype=np.float32)
    cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1)
    # class-aware NMS via per-class coordinate offset
    off = xyxy + cls[:, None] * 7680
    idx = cv2.dnn.NMSBoxes(
        [[b[0], b[1], b[2] - b[0], b[3] - b[1]] for b in off.tolist()], conf.tolist(), conf_thr, iou_thr
    )
    idx = np.array(idx).reshape(-1)
    xyxy = (xyxy[idx] - [dx, dy, dx, dy]) / r
    return np.concatenate([xyxy, conf[idx, None], cls[idx, None]], 1)


def iou(a, b):
    x1, y1 = np.maximum(a[0], b[0]), np.maximum(a[1], b[1])
    x2, y2 = np.minimum(a[2], b[2]), np.minimum(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def match(ref, test, thr=0.5):
    used, pairs = set(), []
    for i in np.argsort(-ref[:, 4]):
        best, bj = 0.0, -1
        for j in range(len(test)):
            if j in used or test[j, 5] != ref[i, 5]:
                continue
            v = iou(ref[i], test[j])
            if v > best:
                best, bj = v, j
        if best >= thr:
            used.add(bj)
            pairs.append((best, abs(ref[i, 4] - test[bj, 4])))
    return pairs


def main():
    args = parse_args()
    m = onnx.load(args.onnx)
    onnx.checker.check_model(m)
    opset = next(o.version for o in m.opset_import if o.domain in ("", "ai.onnx"))

    sess = ort.InferenceSession(args.onnx, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    print(f"onnx: opset={opset} input={inp.name}{inp.shape} output={sess.get_outputs()[0].shape}")

    yolo = YOLO(args.pt)
    torch_model = yolo.model.float().eval().fuse()
    names = yolo.names

    cap = cv2.VideoCapture(args.source)
    report = {"onnx": args.onnx, "opset": opset, "input_shape": inp.shape, "frames": []}
    all_pairs, ort_times = [], []
    for f in args.frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, img = cap.read()
        if not ok:
            continue
        x, r, dx, dy = preprocess(img, args.imgsz)

        t0 = time.perf_counter()
        out = sess.run(None, {inp.name: x})[0]
        ort_times.append((time.perf_counter() - t0) * 1000)
        with torch.no_grad():
            ref_out = torch_model(torch.from_numpy(x))[0].numpy()
        raw_diff = float(np.abs(out - ref_out).max())

        det_ort = postprocess(out, r, dx, dy, args.conf, args.iou)
        res = yolo.predict(img, conf=args.conf, iou=args.iou, imgsz=args.imgsz, device="cpu", verbose=False)[0]
        det_pt = np.concatenate(
            [res.boxes.xyxy.numpy(), res.boxes.conf.numpy()[:, None], res.boxes.cls.numpy()[:, None]], 1
        )
        pairs = match(det_pt, det_ort)
        all_pairs += pairs
        row = {
            "frame": f,
            "raw_max_abs_diff": round(raw_diff, 5),
            "n_pt": len(det_pt),
            "n_onnx": len(det_ort),
            "matched": len(pairs),
            "persons_pt": int((det_pt[:, 5] == 0).sum()),
            "persons_onnx": int((det_ort[:, 5] == 0).sum()),
            "mean_iou": round(float(np.mean([p[0] for p in pairs])), 4) if pairs else None,
        }
        report["frames"].append(row)
        print(row)

    report["summary"] = {
        "ort_inference_ms_mean": round(float(np.mean(ort_times[1:] or ort_times)), 2),
        "matched_total": len(all_pairs),
        "pt_total": sum(r["n_pt"] for r in report["frames"]),
        "onnx_total": sum(r["n_onnx"] for r in report["frames"]),
        "mean_iou": round(float(np.mean([p[0] for p in all_pairs])), 4) if all_pairs else None,
        "mean_conf_abs_diff": round(float(np.mean([p[1] for p in all_pairs])), 4) if all_pairs else None,
        "max_raw_abs_diff": max(r["raw_max_abs_diff"] for r in report["frames"]),
        "classes": sorted({names[int(c)] for c in det_ort[:, 5]}) if len(det_ort) else [],
    }
    print("\n=== Summary ===")
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
