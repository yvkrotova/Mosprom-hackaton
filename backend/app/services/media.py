"""Работа с видео через OpenCV: проверка загруженного файла и кадр-превью."""

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from app.schemas.enums import SourceKind

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}
OPEN_TIMEOUT_MS = 5000


@dataclass(frozen=True, slots=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    frame_count: int


def open_capture(uri: str) -> cv2.VideoCapture:
    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS]
    return cv2.VideoCapture(uri, cv2.CAP_ANY, params)


def probe_video(path: Path) -> VideoInfo | None:
    """None, если файл не открывается как видео или в нём нет кадров."""
    cap = open_capture(str(path))
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        return VideoInfo(
            width=frame.shape[1],
            height=frame.shape[0],
            fps=cap.get(cv2.CAP_PROP_FPS) or 0.0,
            frame_count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
        )
    finally:
        cap.release()


def _first_frame(kind: str, uri: str) -> np.ndarray | None:
    if kind == SourceKind.IMAGES:
        folder = Path(uri)
        files = sorted(p for p in folder.glob("*") if p.suffix.lower() in IMAGE_EXTENSIONS)
        return cv2.imread(str(files[0])) if files else None
    cap = open_capture(uri)
    try:
        if not cap.isOpened():
            return None
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def preview_jpeg(kind: str, uri: str) -> bytes | None:
    """Первый кадр источника в исходном разрешении, JPEG. None — кадр получить не удалось."""
    frame = _first_frame(kind, uri)
    if frame is None:
        return None
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None
