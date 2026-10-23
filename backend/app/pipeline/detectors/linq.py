"""LinqDetector: модель на ускорителе LinQ. ЗАГЛУШКА — реализует CV-участник.

Сигнатуры Python API LinQ берутся из usage_example_python_api.zip организаторов. Здесь они
НЕ выдуманы: места вызова помечены TODO и бросают NotImplementedError. Всё, что не зависит
от API LinQ (letterbox, NMS, обратное масштабирование), уже реализовано ниже.

Что происходит в infer(frame) (frame.image — BGR uint8, (H, W, 3), как отдаёт OpenCV):

1. Препроцессинг (_preprocess):
   - letterbox до входа сети (например 640x640): масштаб с сохранением пропорций + паддинг;
   - BGR -> RGB, если сеть обучалась на RGB;
   - HWC -> CHW, добавить ось батча;
   - тип и нормализация как при компиляции под LinQ (float /255, mean/std или int8-квантование).
2. Инференс (_run): вызов скомпилированной программы TPU через Python API LinQ.
3. Постпроцессинг (_postprocess):
   - декодирование выхода сети (сетка/якоря, sigmoid) -> боксы в координатах входа сети,
     уверенность, id класса;
   - порог уверенности и NMS (nms() ниже);
   - обратное масштабирование с учётом letterbox (unletterbox_boxes() ниже) -> пиксели исходного
     кадра, обрезка по границам, деление на W/H -> доли 0..1;
   - id класса -> label ("person" для людей — обязательно).
4. Результат — FrameDetections; inference_ms — время шагов 1–3.

Любая ошибка ускорителя — исключение (воркер покажет статус error), а не пустой список.
Настройки — только из .env: LINQ_API_URL, LINQ_LOGIN, LINQ_PASSWORD, LINQ_PROGRAM.
"""

import time
from typing import Any

import cv2
import numpy as np

from app.config import Settings
from app.pipeline.interfaces import Detection, Detector, Frame, FrameDetections


def letterbox(
    image: np.ndarray, size: int = 640, pad_value: int = 114
) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Вписывает кадр в квадрат size x size. Возвращает (картинка, масштаб, (pad_x, pad_y))."""
    h, w = image.shape[:2]
    scale = min(size / w, size / h)
    new_w, new_h = round(w * scale), round(h * scale)
    resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_x, pad_y = (size - new_w) // 2, (size - new_h) // 2
    canvas = np.full((size, size, 3), pad_value, dtype=image.dtype)
    canvas[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
    return canvas, scale, (pad_x, pad_y)


def unletterbox_boxes(
    boxes_xyxy: np.ndarray, scale: float, pad: tuple[int, int], frame_w: int, frame_h: int
) -> np.ndarray:
    """Боксы из координат входа сети -> доли 0..1 исходного кадра."""
    boxes = boxes_xyxy.astype(np.float32).copy()
    boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad[0]) / scale
    boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad[1]) / scale
    boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, frame_w) / frame_w
    boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, frame_h) / frame_h
    return boxes


def nms(boxes_xyxy: np.ndarray, scores: np.ndarray, iou_threshold: float = 0.45) -> list[int]:
    """Индексы боксов, оставшихся после NMS."""
    if len(boxes_xyxy) == 0:
        return []
    xywh = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in boxes_xyxy]
    keep = cv2.dnn.NMSBoxes(xywh, [float(s) for s in scores], 0.0, iou_threshold)
    return [int(i) for i in np.array(keep).flatten()]


class LinqDetector(Detector):
    kind = "linq"

    INPUT_SIZE = 640
    CONF_THRESHOLD = 0.25
    CLASS_NAMES: dict[int, str] = {0: "person"}  # TODO: таблица классов вашей модели

    def __init__(self, settings: Settings) -> None:
        self.api_url = settings.linq_api_url
        self.program = settings.linq_program
        self.model_version = f"linq:{self.program or 'unknown'}"
        # TODO(CV): подключиться к LinQ и загрузить программу через Python API организаторов
        #   (usage_example_python_api.zip). Логин/пароль — settings.linq_login / linq_password,
        #   только из .env.
        self._session: Any = None

    def _preprocess(self, image: np.ndarray) -> tuple[np.ndarray, float, tuple[int, int]]:
        canvas, scale, pad = letterbox(image, self.INPUT_SIZE)
        # TODO(CV): порядок каналов, раскладка и нормализация — как при компиляции модели.
        tensor = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).transpose(2, 0, 1)[None]
        return tensor, scale, pad

    def _run(self, tensor: np.ndarray) -> Any:
        # TODO(CV): вызов программы TPU через Python API LinQ; вернуть сырые выходы сети.
        raise NotImplementedError(
            "LinqDetector._run: вызов LinQ не реализован — см. usage_example_python_api.zip"
        )

    def _decode(self, raw: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        # TODO(CV): декодировать выход модели -> (boxes_xyxy во входе сети, scores, class_ids).
        raise NotImplementedError(
            "LinqDetector._decode: декодирование выхода модели не реализовано"
        )

    def infer(self, frame: Frame) -> FrameDetections:
        if frame.image is None:
            raise ValueError("LinqDetector нужен кадр с изображением (не NullFrameSource)")
        t0 = time.perf_counter()
        tensor, scale, pad = self._preprocess(frame.image)
        boxes, scores, class_ids = self._decode(self._run(tensor))

        mask = scores >= self.CONF_THRESHOLD
        boxes, scores, class_ids = boxes[mask], scores[mask], class_ids[mask]
        keep = nms(boxes, scores)
        rel = unletterbox_boxes(boxes[keep], scale, pad, frame.width, frame.height)
        detections = [
            Detection(
                label=self.CLASS_NAMES.get(int(cls), str(int(cls))),
                confidence=float(score),
                bbox=tuple(float(v) for v in box),
            )
            for box, score, cls in zip(rel, scores[keep], class_ids[keep], strict=True)
            if box[2] > box[0] and box[3] > box[1]
        ]
        return FrameDetections(
            source_id=frame.source_id,
            frame_idx=frame.frame_idx,
            video_ts=frame.video_ts,
            frame_w=frame.width,
            frame_h=frame.height,
            inference_ms=(time.perf_counter() - t0) * 1000,
            model_version=self.model_version,
            detections=detections,
        )

    def close(self) -> None:
        # TODO(CV): освободить ускоритель/сессию LinQ.
        self._session = None
