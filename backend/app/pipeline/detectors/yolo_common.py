"""Пре- и постпроцессинг YOLOv8 (detect) без зависимостей от рантайма инференса.

Общий код для OnnxDetector (onnxruntime на CPU) и LinqDetector (ускоритель LinQ):
одинаковые letterbox и декодирование выхода гарантируют, что результаты сравнимы.

Выход YOLOv8 detect без NMS: [1, 4 + C, N] (для COCO 640x640: [1, 84, 8400]).
Первые 4 строки — cx, cy, w, h в пикселях входа сети; остальные C — оценки классов
(уже после sigmoid, objectness у v8 нет).
"""

import ast
from collections.abc import Iterable
from dataclasses import dataclass

import cv2
import numpy as np

from app.pipeline.interfaces import Detection


@dataclass(frozen=True, slots=True)
class LetterboxInfo:
    """Как исходный кадр вписан во вход сети — нужно, чтобы вернуть боксы обратно."""

    scale: float
    pad_x: int
    pad_y: int
    frame_w: int
    frame_h: int


def letterbox(
    image_bgr: np.ndarray, size: int = 640, pad_value: int = 114
) -> tuple[np.ndarray, LetterboxInfo]:
    """BGR uint8 (H, W, 3) -> тензор [1, 3, size, size] float32, RGB, 0..1.

    Масштаб с сохранением пропорций, кадр по центру, поля залиты pad_value (как в ultralytics).
    """
    h, w = image_bgr.shape[:2]
    scale = min(size / w, size / h)
    new_w, new_h = round(w * scale), round(h * scale)
    resized = cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_x, pad_y = (size - new_w) // 2, (size - new_h) // 2
    canvas = np.full((size, size, 3), pad_value, dtype=np.uint8)
    canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    rgb = canvas[:, :, ::-1]
    tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
    return tensor, LetterboxInfo(scale, pad_x, pad_y, w, h)


def nms(boxes_xyxy: np.ndarray, scores: np.ndarray, iou_threshold: float) -> np.ndarray:
    """Классический NMS. Возвращает индексы оставшихся боксов по убыванию score."""
    order = scores.argsort()[::-1]
    x1, y1, x2, y2 = boxes_xyxy.T
    areas = (x2 - x1).clip(0) * (y2 - y1).clip(0)
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        rest = order[1:]
        iw = (np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest])).clip(0)
        ih = (np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest])).clip(0)
        inter = iw * ih
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_threshold]
    return np.array(keep, dtype=np.int64)


def postprocess(
    output: np.ndarray,
    info: LetterboxInfo,
    names: dict[int, str],
    *,
    conf_threshold: float = 0.35,
    iou_threshold: float = 0.45,
    class_ids: Iterable[int] | None = None,
    max_det: int = 300,
) -> list[Detection]:
    """Сырой выход YOLOv8 -> детекции в долях 0..1 исходного кадра.

    Шаги: транспонирование, лучший класс, порог, фильтр классов, cxcywh -> xyxy,
    NMS отдельно по каждому классу, снятие паддинга и масштаба, обрезка по кадру, нормализация.
    """
    pred = np.asarray(output, dtype=np.float32)
    if pred.ndim == 3:
        pred = pred[0]
    if pred.ndim != 2 or pred.shape[0] < 5:
        raise ValueError(f"Неожиданная форма выхода YOLO: {np.shape(output)}")
    pred = pred.T  # (N, 4 + C)

    class_scores = pred[:, 4:]
    cls = class_scores.argmax(axis=1)
    conf = class_scores[np.arange(len(cls)), cls]
    mask = conf >= conf_threshold
    if class_ids is not None:
        mask &= np.isin(cls, list(class_ids))
    if not mask.any():
        return []
    boxes, conf, cls = pred[mask, :4], conf[mask], cls[mask]

    cx, cy, bw, bh = boxes.T
    xyxy = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)

    # NMS по классам: сдвигаем боксы разных классов так, чтобы они не пересекались.
    offset = cls[:, None].astype(np.float32) * (xyxy.max() + 1.0)
    keep = nms(xyxy + offset, conf, iou_threshold)[:max_det]
    xyxy, conf, cls = xyxy[keep], conf[keep], cls[keep]

    xyxy[:, [0, 2]] = ((xyxy[:, [0, 2]] - info.pad_x) / info.scale).clip(0, info.frame_w)
    xyxy[:, [1, 3]] = ((xyxy[:, [1, 3]] - info.pad_y) / info.scale).clip(0, info.frame_h)
    xyxy[:, [0, 2]] /= info.frame_w
    xyxy[:, [1, 3]] /= info.frame_h

    return [
        Detection(
            label=names.get(int(c), str(int(c))),
            confidence=round(float(s), 4),
            bbox=tuple(round(float(v), 5) for v in box),
        )
        for box, s, c in zip(xyxy, conf, cls, strict=True)
        if box[2] - box[0] > 1e-4 and box[3] - box[1] > 1e-4
    ]


def parse_names(raw: str | dict | None) -> dict[int, str]:
    """Имена классов из metadata модели ultralytics: строка вида "{0: 'person', ...}"."""
    if not raw:
        return {}
    data = ast.literal_eval(raw) if isinstance(raw, str) else raw
    return {int(k): str(v) for k, v in data.items()}


def resolve_class_ids(labels: Iterable[str], names: dict[int, str]) -> set[int] | None:
    """DETECT_CLASSES ("person,car") -> id классов. Пустой список — все классы (None)."""
    wanted = [label.strip() for label in labels if label.strip()]
    if not wanted:
        return None
    by_name = {name: idx for idx, name in names.items()}
    unknown = [label for label in wanted if label not in by_name]
    if unknown:
        raise ValueError(f"Классов {unknown} нет в модели (есть: {sorted(by_name)[:10]}…)")
    return {by_name[label] for label in wanted}
