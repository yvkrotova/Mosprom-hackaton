"""Run with /opt/conda/bin/python -u linq_bridge_server.py in Jupyter."""

import os

os.environ["OMP_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import cv2
import numpy as np
import pika
from tpu_pycloud_format import (
    MESSAGE_ID_ATTR_NAME,
    RESPONSE_ROUTING_KEYS_ATTR_NAME,
    TARGET_URI_ATTR_NAME,
    PyTask,
    deserialize,
    serialize,
)

cv2.setNumThreads(1)


def _env(name, default=None):
    """Адрес, учётные данные и путь программы — только из окружения, не из кода."""
    value = os.environ.get(name, default)
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name} (см. bridge_update/README.txt)")
    return value


PROGRAM = Path(_env("LINQ_PROGRAM"))
AMQP_HOST = _env("LINQ_AMQP_HOST")
AMQP_PORT = int(_env("LINQ_AMQP_PORT", "5672"))
AMQP_LOGIN = _env("LINQ_LOGIN")
AMQP_PASSWORD = _env("LINQ_PASSWORD")
NAMES = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


class TPU:
    connection = None

    def close(self):
        c = self.connection
        self.connection = None
        if c is not None and c.is_open:
            try:  # noqa: SIM105 — файл запускается в Jupyter как есть, без лишних импортов
                c.close()
            except Exception:
                pass

    def run(self, tensor):
        try:
            if self.connection is None or not self.connection.is_open:
                self.connection = pika.BlockingConnection(
                    pika.ConnectionParameters(
                        host=AMQP_HOST,
                        port=AMQP_PORT,
                        credentials=pika.PlainCredentials(AMQP_LOGIN, AMQP_PASSWORD),
                        socket_timeout=10,
                        stack_timeout=20,
                        blocked_connection_timeout=20,
                        heartbeat=60,
                    )
                )
                self.channel = self.connection.channel()
                self.queue = "notebook_" + uuid.uuid4().hex
                self.channel.queue_declare(queue=self.queue, exclusive=True, auto_delete=True)
                self.channel.queue_bind(
                    exchange="tpu.direct", queue=self.queue, routing_key=self.queue
                )
            task = uuid.uuid4().hex
            self.channel.basic_publish(
                exchange="tpu.direct",
                routing_key="input",
                body=serialize(PyTask({"images": tensor})),
                properties=pika.BasicProperties(
                    headers={
                        TARGET_URI_ATTR_NAME: f"file://{PROGRAM}?raw=False",
                        RESPONSE_ROUTING_KEYS_ATTR_NAME: json.dumps([self.queue]),
                        MESSAGE_ID_ATTR_NAME: task,
                    }
                ),
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                method, props, body = self.channel.basic_get(queue=self.queue, auto_ack=True)
                if method is None:
                    self.connection.process_data_events(time_limit=0.01)
                    continue
                if (props.headers or {}).get(MESSAGE_ID_ATTR_NAME) != task:
                    raise RuntimeError("Response ID mismatch")
                data, _, worker = deserialize(body)
                if worker.get("status_code") != 0 or not data or "output0" not in data:
                    raise RuntimeError(str(worker))
                arr = np.asarray(data["output0"])
                if (
                    arr.shape != (1, 84, 8400)
                    or arr.dtype != np.float32
                    or not np.isfinite(arr).all()
                ):
                    raise ValueError("Invalid TPU output")
                return arr, float(worker.get("inference_time") or 0) * 1000
            raise TimeoutError("TPU response timeout")
        except Exception:
            self.close()
            raise


tpu = TPU()


def infer(image, conf, iou, labels):
    h, w = image.shape[:2]
    scale = min(640 / w, 640 / h)
    nw, nh = round(w * scale), round(h * scale)
    px, py = (640 - nw) // 2, (640 - nh) // 2
    canvas = np.full((640, 640, 3), 114, np.uint8)
    canvas[py : py + nh, px : px + nw] = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
    tensor = (
        np.ascontiguousarray(canvas[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
    )
    start = time.monotonic()
    output, device = tpu.run(tensor)
    request_ms = (time.monotonic() - start) * 1000
    pred = output[0].T
    classes = pred[:, 4:].argmax(axis=1)
    scores = pred[:, 4:].max(axis=1)
    wanted = {k for k, v in NAMES.items() if v in labels} if labels else set(range(80))
    mask = (scores >= conf) & np.isin(classes, list(wanted))
    pred, scores, classes = pred[mask], scores[mask], classes[mask]
    boxes = np.empty((len(pred), 4), np.float32)
    boxes[:, :2] = pred[:, :2] - pred[:, 2:4] / 2
    boxes[:, 2:] = pred[:, :2] + pred[:, 2:4] / 2
    keep = []
    for cls in np.unique(classes):
        order = np.where(classes == cls)[0]
        order = order[np.argsort(scores[order])[::-1]]
        while order.size:
            cur = int(order[0])
            keep.append(cur)
            rest = order[1:]
            if not rest.size:
                break
            lt = np.maximum(boxes[cur, :2], boxes[rest, :2])
            rb = np.minimum(boxes[cur, 2:], boxes[rest, 2:])
            wh = np.maximum(rb - lt, 0)
            inter = wh[:, 0] * wh[:, 1]
            a = np.maximum(boxes[cur, 2:] - boxes[cur, :2], 0)
            b = np.maximum(boxes[rest, 2:] - boxes[rest, :2], 0)
            union = a[0] * a[1] + b[:, 0] * b[:, 1] - inter
            order = rest[inter / (union + 1e-9) <= iou]
    detections = []
    for idx in sorted(keep, key=lambda k: float(scores[k]), reverse=True)[:300]:
        box = boxes[idx].copy()
        box[[0, 2]] = np.clip((box[[0, 2]] - px) / scale / w, 0, 1)
        box[[1, 3]] = np.clip((box[[1, 3]] - py) / scale / h, 0, 1)
        if box[2] - box[0] > 1e-4 and box[3] - box[1] > 1e-4:
            detections.append(
                dict(
                    label=NAMES.get(int(classes[idx]), str(int(classes[idx]))),
                    confidence=round(float(scores[idx]), 4),
                    bbox=[round(float(x), 5) for x in box],
                )
            )
    return dict(detections=detections, device_ms=device, server_tpu_request_ms=request_ms)


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.respond(200, dict(status="ok", service="linq-jpeg-bridge", protocol_version=2))

    def do_POST(self):
        start = time.monotonic()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            conf = float(self.headers.get("X-Confidence", "0.1"))
            iou = float(self.headers.get("X-Iou", "0.45"))
            labels = [
                x.strip() for x in self.headers.get("X-Classes", "person").split(",") if x.strip()
            ]
            if not 0 < length <= 4000000 or not 0 <= conf <= 1 or not 0 <= iou <= 1:
                self.respond(400, {"error": "Invalid size or thresholds"})
                return
            if any(x not in NAMES.values() for x in labels):
                self.respond(400, {"error": "Unsupported class"})
                return
            payload = self.rfile.read(length)
            image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.shape[0] * image.shape[1] > 12000000:
                self.respond(400, {"error": "Invalid image"})
                return
            result = infer(image, conf, iou, labels)
            result.update(
                protocol_version=2,
                program=PROGRAM.name,
                frame_w=image.shape[1],
                frame_h=image.shape[0],
                conf_threshold=conf,
                iou_threshold=iou,
                server_total_ms=(time.monotonic() - start) * 1000,
            )
            self.respond(200, result)
            print(
                f"OK server_ms={result['server_total_ms']:.1f} boxes={len(result['detections'])}",
                flush=True,
            )
        except Exception as exc:
            self.respond(502, {"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    if not PROGRAM.is_file():
        raise FileNotFoundError(PROGRAM)
    server = HTTPServer(("127.0.0.1", 8787), Handler)
    print("JPEG bridge v2 ready on port 8787. Keep terminal open.", flush=True)
    try:
        server.serve_forever()
    finally:
        tpu.close()
        server.server_close()
