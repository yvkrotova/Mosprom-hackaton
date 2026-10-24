"""YOLOv8s on LinQ via organizers' RabbitMQ protocol.

raw=False accepts float32 RGB NCHW /255 and returns float32 [1,84,8400].
Server handles quantization; do not quantize the input a second time.
The existing pipeline's preprocessing, tracker and scenario rules are retained.
"""

import time
from typing import Any

import numpy as np

from app.config import Settings
from app.pipeline.detectors.yolo_common import (
    dequantize,
    letterbox,
    postprocess,
    quantize,
    resolve_class_ids,
)
from app.pipeline.interfaces import Detector, Frame, FrameDetections

# Классы COCO, нужные сценариям. Полную таблицу берите из metadata модели (yolo_common.parse_names),
# если программа LinQ её сохраняет.
COCO_NAMES: dict[int, str] = {0: "person", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


class LinqDetector(Detector):
    kind = "linq_tpu"

    def __init__(self, settings: Settings) -> None:
        self.api_url = settings.linq_api_url
        self.program = settings.linq_program
        self.model_version = f"linq:{self.program or 'unknown'}"
        self.input_size = settings.model_input_size
        self.names = COCO_NAMES
        self.class_ids = resolve_class_ids(settings.detect_class_list, self.names)
        self.conf_threshold = settings.detector_conf_threshold
        self.iou_threshold = settings.iou_threshold
        from app.pipeline.detectors.linq_transport import LinqTransport

        self._session = LinqTransport(settings)
        self.input_quant: tuple[float, int] | None = None
        self.output_quant: tuple[float, int] | None = None

    def _to_device_input(self, tensor: np.ndarray) -> Any:
        if self.input_quant is None:
            return tensor
        return quantize(tensor, *self.input_quant)

    def _run(self, device_input: Any) -> Any:
        if self.input_size != 640:
            raise ValueError("Выбранная TPU-программа требует MODEL_INPUT_SIZE=640")
        return self._session.run(device_input)

    def _dequantize(self, raw: Any) -> np.ndarray:
        """Выход TPU -> float32 [1, 84, 8400]. int8/uint8 требует output_quant."""
        arr = np.asarray(raw)
        if np.issubdtype(arr.dtype, np.integer):
            if self.output_quant is None:
                raise ValueError(
                    f"Выход LinQ целочисленный ({arr.dtype}), но не заданы scale/zero_point выхода "
                    "(LinqDetector.output_quant) — возьмите их из отчёта компилятора"
                )
            return dequantize(arr, *self.output_quant)
        return arr.astype(np.float32, copy=False)

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
        self._session.close()
