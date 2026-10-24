import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import cv2

from app.config import Settings
from app.pipeline.detectors import build_detector
from app.pipeline.interfaces import Frame

image = cv2.imread("test2.jfif")
if image is None:
    raise SystemExit("Place test2.jfif in backend")
s = Settings()
if not s.linq_bridge_url:
    raise SystemExit("LINQ_BRIDGE_URL is empty")
d = build_detector("linq", s)
print("Adapter:", type(d).__name__, flush=True)
h, w = image.shape[:2]
try:
    for i in range(3):
        result = d.infer(Frame("bridge-test", i, float(i), w, h, image))
        print(
            f"Request {i + 1}: {result.inference_ms:.1f} ms; device={d.device_ms}; "
            f"boxes={len(result.detections)}",
            flush=True,
        )
finally:
    d.close()
