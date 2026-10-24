"""LinqDetector: YOLOv8 на ускорителе LinQ. Вызов ускорителя — ЗАГЛУШКА (TODO).

Сигнатуры Python API LinQ берутся из usage_example_python_api.zip организаторов. Здесь они
НЕ выдуманы: места вызова помечены TODO и бросают NotImplementedError. Пре- и постпроцессинг
общие с OnnxDetector (yolo_common.py), поэтому результаты LinQ и CPU напрямую сравнимы.

Что происходит в infer(frame) (frame.image — BGR uint8, (H, W, 3), как отдаёт OpenCV):

1. letterbox() -> тензор [1, 3, 640, 640] float32, RGB, 0..1 (как при экспорте в ONNX).
   Если программа LinQ скомпилирована с int8-входом, тензор нужно квантовать
   (q = round(x / scale) + zero_point) параметрами из компилятора — TODO(CV) в _to_device_input.
2. _run(): вызов скомпилированной программы TPU -> сырой выход сети.
3. Выход после квантования может прийти в int8/uint8: его нужно деквантовать
   (x = (q - zero_point) * scale) до float32 формы [1, 84, 8400] — TODO(CV) в _dequantize.
   Модель экспортирована без NMS: 4 координаты cx, cy, w, h в пикселях входа + 80 оценок классов.
4. postprocess(): порог, фильтр классов, NMS, снятие letterbox, доли 0..1.

Любая ошибка ускорителя — исключение (воркер покажет статус error), а не пустой список.
Доступ — только из .env: LINQ_API_URL, LINQ_LOGIN, LINQ_PASSWORD, LINQ_PROGRAM.
"""

import time
from typing import Any

import numpy as np

from app.config import Settings
from app.pipeline.detectors.yolo_common import letterbox, postprocess, resolve_class_ids
from app.pipeline.interfaces import Detector, Frame, FrameDetections

# Классы COCO, нужные сценариям. Полную таблицу берите из metadata модели (yolo_common.parse_names),
# если программа LinQ её сохраняет.
COCO_NAMES: dict[int, str] = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


class LinqDetector(Detector):
    kind = "linq"

    def __init__(self, settings: Settings) -> None:
        self.api_url = settings.linq_api_url
        self.program = settings.linq_program
        self.model_version = f"linq:{self.program or 'unknown'}"
        self.input_size = settings.model_input_size
        self.names = COCO_NAMES
        self.class_ids = resolve_class_ids(settings.detect_class_list, self.names)
        self.conf_threshold = settings.conf_threshold
        self.iou_threshold = settings.iou_threshold
        # TODO(CV): подключиться к LinQ и загрузить программу через Python API организаторов
        #   (usage_example_python_api.zip). Логин/пароль — settings.linq_login / linq_password,
        #   только из .env.
        self._session: Any = None

    def _to_device_input(self, tensor: np.ndarray) -> Any:
        # TODO(CV): если вход программы int8 — квантовать параметрами компилятора LinQ.
        return tensor

    def _run(self, device_input: Any) -> Any:
        # TODO(CV): вызов программы TPU через Python API LinQ; вернуть сырой выход сети.
        raise NotImplementedError(
            "LinqDetector._run: вызов LinQ не реализован — см. usage_example_python_api.zip"
        )

    def _dequantize(self, raw: Any) -> np.ndarray:
        # TODO(CV): int8/uint8 -> float32 (x = (q - zero_point) * scale), форма [1, 84, 8400].
        return np.asarray(raw, dtype=np.float32)

    def infer(self, frame: Frame) -> FrameDetections:
        if frame.image is None:
            raise ValueError("LinqDetector нужен кадр с изображением (не NullFrameSource)")
        t0 = time.perf_counter()
        tensor, info = letterbox(frame.image, self.input_size)
        device_input = self._to_device_input(tensor)
        t1 = time.perf_counter()
        raw = self._run(device_input)
        t2 = time.perf_counter()
        detections = postprocess(
            self._dequantize(raw),
            info,
            self.names,
            conf_threshold=self.conf_threshold,
            iou_threshold=self.iou_threshold,
            class_ids=self.class_ids,
        )
        t3 = time.perf_counter()
        return FrameDetections(
            source_id=frame.source_id,
            frame_idx=frame.frame_idx,
            video_ts=frame.video_ts,
            frame_w=frame.width,
            frame_h=frame.height,
            inference_ms=(t2 - t1) * 1000,
            preprocess_ms=(t1 - t0) * 1000,
            postprocess_ms=(t3 - t2) * 1000,
            model_version=self.model_version,
            detections=detections,
        )

    def close(self) -> None:
        # TODO(CV): освободить ускоритель/сессию LinQ.
        self._session = None
