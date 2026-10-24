"""Кадр-доказательство: рамки, полигоны зон, траектория и подпись сценария и времени.

Подписи рисуются через Pillow шрифтом с кириллицей (Arial/DejaVu). Если ни Pillow, ни шрифта
нет — встроенным шрифтом OpenCV с транслитерацией.
"""

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.pipeline.interfaces import Track, ZoneGeometry

RED = (40, 40, 230)
GREEN = (80, 200, 80)
GRAY = (170, 170, 170)
YELLOW = (0, 210, 250)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
MIN_SIDE = 320  # кадр-доказательство меньше этого по короткой стороне увеличивается

FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

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


@lru_cache(maxsize=8)
def _font(size: int):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return None


def draw_texts(
    canvas: np.ndarray, items: list[tuple[str, tuple[int, int], int, tuple, tuple | None]]
) -> None:
    """Подписи [(текст, (x, y) левый верх, размер px, цвет BGR, фон BGR | None)] прямо на canvas."""
    if not items:
        return
    font_ok = all(_font(size) for _, _, size, _, _ in items)
    if font_ok:
        from PIL import Image, ImageDraw

        img = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(img)
        for text, (x, y), size, color, bg in items:
            font = _font(size)
            # Подпись не должна вылезать за правый край кадра.
            overflow = draw.textbbox((x, y), text, font=font)[2] - (img.width - 6)
            if overflow > 0:
                x = max(4, x - overflow)
            if bg is not None:
                left, top, right, bottom = draw.textbbox((x, y), text, font=font)
                draw.rectangle((left - 4, top - 3, right + 4, bottom + 3), fill=bg[::-1])
            draw.text((x, y), text, font=font, fill=color[::-1])
        canvas[:] = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
        return
    for text, (x, y), size, color, bg in items:
        scale = size / 30
        txt = translit(text)
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        if bg is not None:
            cv2.rectangle(canvas, (x - 4, y - 3), (x + tw + 4, y + th + 6), bg, -1)
        cv2.putText(
            canvas, txt, (x, y + th), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA
        )


def _px(points: Sequence[Sequence[float]], w: int, h: int) -> np.ndarray:
    return np.array([[round(x * w), round(y * h)] for x, y in points], dtype=np.int32)


# Стиль фигур сценария (RuleHit.extra["shapes"]) — одинаково для любого сценария.
SHAPE_STYLES = {"candidate": (YELLOW, 2), "violation": (RED, 3), "info": (GRAY, 1)}


def annotate_frame(
    image: np.ndarray | None,
    width: int,
    height: int,
    zones: Sequence[ZoneGeometry],
    tracks: Sequence[Track],
    violator_track_id: int | None,
    violation_zone_id: str | None,
    caption_lines: Sequence[str],
    *,
    violator_label: str | None = None,
    trail: Sequence[tuple[float, float]] | None = None,
    shapes: Sequence[dict[str, Any]] | None = None,
) -> np.ndarray:
    """Копия кадра с разметкой — единая для всех сценариев. Без изображения рисует на сером фоне.

    - зоны: зелёный контур, если свободны; зона нарушения — полупрозрачная красная заливка;
    - нарушитель (violator_track_id) — красная рамка и крупная подпись violator_label;
      остальные люди — тонкие серые рамки (видны, но не нарушают);
    - trail — хвост траектории ног нарушителя в долях кадра;
    - shapes — фигуры сценария [{"polygon"|"bbox", "style", "label"}], style —
      candidate — жёлтый контур, violation — красный, info — тонкий серый.
    """
    canvas = image.copy() if image is not None else np.full((height, width, 3), 50, dtype=np.uint8)
    h, w = canvas.shape[:2]
    if min(w, h) < MIN_SIDE:
        # Маленький кадр (например 160×120) увеличиваем: иначе подписи закрывают картинку.
        k = MIN_SIDE / min(w, h)
        canvas = cv2.resize(canvas, (round(w * k), round(h * k)), interpolation=cv2.INTER_LINEAR)
        h, w = canvas.shape[:2]
    thickness = max(2, round(min(w, h) / 300))
    font_px = max(14, round(min(w, h) / 32))

    overlay = canvas.copy()
    for zone in zones:
        pts = _px(zone.polygon, w, h)
        violated = zone.id == violation_zone_id
        if violated:
            cv2.fillPoly(overlay, [pts], RED)
        cv2.polylines(canvas, [pts], isClosed=True, color=RED if violated else GREEN,
                      thickness=thickness)  # fmt: skip
    cv2.addWeighted(overlay, 0.3, canvas, 0.7, 0, dst=canvas)

    texts = []
    for shape in shapes or []:
        color, line = SHAPE_STYLES.get(shape.get("style", "info"), SHAPE_STYLES["info"])
        if shape.get("polygon"):
            pts = _px(shape["polygon"], w, h)
            cv2.polylines(canvas, [pts], isClosed=True, color=color, thickness=line + thickness - 2)
            x, y = pts[:, 0].min(), pts[:, 1].min()
        elif shape.get("bbox"):
            x1, y1, x2, y2 = _px([shape["bbox"][:2], shape["bbox"][2:]], w, h).flatten()
            cv2.rectangle(canvas, (x1, y1), (x2, y2), color, line + thickness - 2)
            x, y = x1, y1
        else:
            continue
        if shape.get("label"):
            size = round(font_px * 0.85)
            texts.append((shape["label"], (int(x), max(0, int(y) - size - 8)), size, WHITE, color))

    if trail and len(trail) > 1:
        pts = _px(trail, w, h)
        for i in range(1, len(pts)):
            cv2.line(canvas, tuple(pts[i - 1]), tuple(pts[i]), RED, thickness + 1, cv2.LINE_AA)
        for p in pts:
            cv2.circle(canvas, tuple(p), thickness + 1, WHITE, -1, cv2.LINE_AA)

    for track in tracks:
        is_violator = violator_track_id is not None and track.track_id == violator_track_id
        x1, y1, x2, y2 = _px([track.bbox[:2], track.bbox[2:]], w, h).flatten()
        if not is_violator:
            cv2.rectangle(canvas, (x1, y1), (x2, y2), GRAY, 1)
            continue
        cv2.rectangle(canvas, (x1, y1), (x2, y2), RED, thickness + 1)
        label = f"#{track.track_id} {violator_label}" if violator_label else f"#{track.track_id}"
        texts.append((label, (x1, max(0, y1 - font_px - 8)), font_px, WHITE, RED))

    line_h = round(font_px * 1.35)
    cv2.rectangle(canvas, (0, 0), (w, line_h * len(caption_lines) + 10), BLACK, -1)
    for i, line in enumerate(caption_lines):
        texts.append((line, (8, 6 + line_h * i), font_px, WHITE, None))
    draw_texts(canvas, texts)
    return canvas


def encode_jpeg(image: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Не удалось закодировать кадр в JPEG")
    return buf.tobytes()
