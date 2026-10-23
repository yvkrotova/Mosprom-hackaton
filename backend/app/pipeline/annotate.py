"""Кадр-доказательство: рамки, полигоны зон и подпись сценария и времени.

Встроенный шрифт OpenCV не умеет кириллицу, поэтому подписи транслитерируются.
"""

from collections.abc import Sequence

import cv2
import numpy as np

from app.pipeline.interfaces import Track, ZoneGeometry

RED = (40, 40, 230)
GREEN = (80, 200, 80)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "№": "N", "«": '"', "»": '"', "—": "-", "–": "-",
}  # fmt: skip


def translit(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in _TRANSLIT:
            rep = _TRANSLIT[low]
            out.append(rep.capitalize() if ch != low else rep)
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def _px(points: Sequence[Sequence[float]], w: int, h: int) -> np.ndarray:
    return np.array([[round(x * w), round(y * h)] for x, y in points], dtype=np.int32)


def annotate_frame(
    image: np.ndarray | None,
    width: int,
    height: int,
    zones: Sequence[ZoneGeometry],
    tracks: Sequence[Track],
    violator_track_id: int | None,
    violation_zone_id: str | None,
    caption_lines: Sequence[str],
) -> np.ndarray:
    """Копия кадра с разметкой. Без изображения (replay без видео) рисует на сером фоне."""
    canvas = image.copy() if image is not None else np.full((height, width, 3), 50, dtype=np.uint8)
    h, w = canvas.shape[:2]
    thickness = max(2, round(min(w, h) / 300))

    overlay = canvas.copy()
    for zone in zones:
        color = RED if zone.id == violation_zone_id else (0, 200, 230)
        pts = _px(zone.polygon, w, h)
        cv2.fillPoly(overlay, [pts], color)
        cv2.polylines(canvas, [pts], isClosed=True, color=color, thickness=thickness)
    cv2.addWeighted(overlay, 0.25, canvas, 0.75, 0, dst=canvas)

    font_scale = max(0.5, min(w, h) / 900)
    for track in tracks:
        color = RED if track.track_id == violator_track_id else GREEN
        x1, y1, x2, y2 = _px([track.bbox[:2], track.bbox[2:]], w, h).flatten()
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, thickness)
        label = f"{track.label} #{track.track_id} {track.confidence:.2f}"
        cv2.putText(
            canvas,
            label,
            (x1, max(15, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale * 0.8,
            color,
            max(1, thickness - 1),
            cv2.LINE_AA,
        )

    line_h = round(28 * font_scale) + 6
    cv2.rectangle(canvas, (0, 0), (w, line_h * len(caption_lines) + 8), BLACK, -1)
    for i, line in enumerate(caption_lines):
        cv2.putText(
            canvas,
            translit(line),
            (8, line_h * (i + 1)),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            WHITE,
            max(1, thickness - 1),
            cv2.LINE_AA,
        )
    return canvas


def encode_jpeg(image: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Не удалось закодировать кадр в JPEG")
    return buf.tobytes()
