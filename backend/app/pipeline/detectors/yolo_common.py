"""Пре- и постпроцессинг YOLOv8 для детекторов backend.

Вся математика — в yolo_core.py (без зависимостей от backend, её же использует скрипт для
Jupyter LinQ). Здесь — только перевод рамок в Detection с координатами в долях кадра 0..1.
Общий код для OnnxDetector (onnxruntime на CPU) и LinqDetector гарантирует сравнимые результаты.
"""

from collections.abc import Iterable

import numpy as np

from app.pipeline.detectors.yolo_core import (
    COCO_NAMES,
    LetterboxInfo,
    decode,
    dequantize,
    letterbox,
    nms,
    parse_names,
    quantize,
    resolve_class_ids,
    to_float_output,
)
from app.pipeline.interfaces import Detection

__all__ = [
    "COCO_NAMES",
    "LetterboxInfo",
    "decode",
    "dequantize",
    "letterbox",
    "nms",
    "parse_names",
    "postprocess",
    "quantize",
    "resolve_class_ids",
    "to_float_output",
]


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
    """Сырой выход YOLOv8 -> детекции в долях 0..1 исходного кадра (см. yolo_core.decode)."""
    w, h = info.frame_w, info.frame_h
    return [
        Detection(
            label=names.get(c, str(c)),
            confidence=round(score, 4),
            bbox=(round(x1 / w, 5), round(y1 / h, 5), round(x2 / w, 5), round(y2 / h, 5)),
        )
        for c, score, (x1, y1, x2, y2) in decode(
            output,
            info,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            class_ids=class_ids,
            max_det=max_det,
        )
    ]
