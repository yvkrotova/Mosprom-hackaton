"""Отправка кадров для живого просмотра — в фоне, без задержки конвейера.

Хранится только самый свежий кадр: если backend или сеть не успевают, старые кадры
отбрасываются (для живого просмотра важна актуальность, а не полнота).
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import cv2

from app.pipeline.annotate import encode_jpeg
from app.pipeline.interfaces import Frame
from app.pipeline.runner import FrameResult

logger = logging.getLogger(__name__)

SendFn = Callable[[dict[str, Any], bytes | None], None]


def frame_meta(frame: Frame, result: FrameResult, fps: float | None) -> dict[str, Any]:
    zones_by_track: dict[int, list[str]] = {}
    for hit in result.hits:
        if hit.zone_id and hit.zone_id not in zones_by_track.setdefault(hit.track_id, []):
            zones_by_track[hit.track_id].append(hit.zone_id)
    tracks = []
    for t in result.tracks:
        if t.track_id in result.alerting_track_ids:
            state = "alert"
        elif t.track_id in zones_by_track:
            state = "in_zone"
        else:
            state = "normal"
        tracks.append({
            "track_id": t.track_id,
            "label": t.label,
            "confidence": round(t.confidence, 3),
            "bbox": [round(v, 4) for v in t.bbox],
            "keypoints": [list(k) for k in t.keypoints] if t.keypoints else None,
            "state": state,
            "zone_ids": zones_by_track.get(t.track_id, []),
        })  # fmt: skip
    return {
        "frame_idx": frame.frame_idx,
        "video_ts": round(frame.video_ts, 3),
        "width": frame.width,
        "height": frame.height,
        "fps": fps,
        "tracks": tracks,
    }


class LiveSender(threading.Thread):
    def __init__(self, send: SendFn, max_width: int = 960, jpeg_quality: int = 70) -> None:
        super().__init__(name="live-sender", daemon=True)
        self._send = send
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self._slot: tuple[dict[str, Any], Any] | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self.sent = 0
        self._last_error_log = 0.0

    def submit(self, meta: dict[str, Any], image: Any) -> None:
        with self._lock:
            self._slot = (meta, image)  # предыдущий неотправленный кадр выбрасывается
        self._ready.set()

    def run(self) -> None:
        while not self._stopped.is_set():
            if not self._ready.wait(0.5):
                continue
            with self._lock:
                slot, self._slot = self._slot, None
                self._ready.clear()
            if slot is None:
                continue
            meta, image = slot
            try:
                self._send(meta, self._encode(image))
                self.sent += 1
            except Exception as exc:
                now = time.monotonic()
                if now - self._last_error_log > 10:  # не засоряем лог на каждом кадре
                    logger.warning("Живой кадр не отправлен: %s", exc)
                    self._last_error_log = now

    def _encode(self, image: Any) -> bytes | None:
        if image is None:
            return None
        h, w = image.shape[:2]
        if w > self.max_width:
            image = cv2.resize(image, (self.max_width, round(h * self.max_width / w)),
                               interpolation=cv2.INTER_AREA)  # fmt: skip
        return encode_jpeg(image, self.jpeg_quality)

    def stop(self) -> None:
        self._stopped.set()
        self._ready.set()
        if self.is_alive():
            self.join(timeout=5)
