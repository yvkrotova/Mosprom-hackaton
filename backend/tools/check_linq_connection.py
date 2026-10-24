"""Run from backend: python -m tools.check_linq_connection [--image PATH]."""

import argparse
import importlib
import socket
from urllib.parse import urlsplit

from app.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Send one real frame to the selected TPU program")
    args = parser.parse_args()
    settings = Settings()
    for name in ("pika", "tpu_pycloud_format"):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise SystemExit(f"Missing dependency: {name}. See docs/LINQ_CONNECT.md") from exc
        print(f"OK import {name}", flush=True)
    address = urlsplit(settings.linq_api_url)
    if address.scheme != "amqp" or not address.hostname:
        raise SystemExit("Set LINQ_API_URL=amqp://HOST:5672/ in backend/.env")
    with socket.create_connection((address.hostname, address.port or 5672), timeout=10):
        print("OK TCP connection to broker (authentication not yet verified)", flush=True)
    if not args.image:
        print("Next: run with --image PATH to verify authentication, TPU and decoding.")
        return
    import cv2

    from app.pipeline.detectors.linq import LinqDetector
    from app.pipeline.interfaces import Frame

    image = cv2.imread(args.image)
    if image is None:
        raise SystemExit(f"Cannot read image: {args.image}")
    h, w = image.shape[:2]
    detector = LinqDetector(settings)
    try:
        result = detector.infer(Frame("check", 0, 0.0, w, h, image))
        print(result.model_dump_json(indent=2))
        print("Worker device_ms:", detector._session.device_ms)
        print("OK: actual TPU request completed; detections may legitimately be empty.")
    finally:
        detector.close()


if __name__ == "__main__":
    main()
