"""OnnxDetector: YOLOv8 через onnxruntime на CPU компьютера (kind="onnx_cpu").

Это НЕ LinQ: детектор нужен, чтобы весь конвейер работал на настоящей нейросети, пока нет
доступа к ускорителю, и как эталон для сравнения точности и скорости с LinQ.

Имена входа/выхода, размер входа и классы читаются из модели. Модель — backend/models/yolov8s.onnx
(как получить — README, раздел «Модель YOLOv8s»).
"""

import time
from pathlib import Path

from app.config import BACKEND_DIR, Settings
from app.pipeline.detectors.yolo_common import (
    letterbox,
    parse_names,
    postprocess,
    resolve_class_ids,
)
from app.pipeline.interfaces import Detector, Frame, FrameDetections


class ModelFileError(RuntimeError):
    pass


def resolve_model_path(raw: str) -> Path:
    """Относительный путь ищется от текущего каталога, затем от каталога backend/."""
    path = Path(raw)
    if not path.is_absolute() and not path.exists():
        path = BACKEND_DIR / path
    if not path.is_file():
        raise ModelFileError(
            f"Файл модели не найден: {raw}. Положите yolov8s.onnx в backend/models/ "
            "или задайте MODEL_PATH в .env (см. README, «Модель YOLOv8s»)"
        )
    return path


class OnnxDetector(Detector):
    kind = "onnx_cpu"

    def __init__(self, settings: Settings) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover — зависит от окружения
            raise ModelFileError("onnxruntime не установлен: pip install onnxruntime") from exc

        path = resolve_model_path(settings.model_path)
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.output_name = self.session.get_outputs()[0].name
        # Статичный вход [1, 3, H, W]; если размер в модели не задан — берём из настроек.
        side = inp.shape[2] if isinstance(inp.shape[2], int) else settings.model_input_size
        self.input_size = int(side)

        meta = self.session.get_modelmeta()
        custom = meta.custom_metadata_map or {}
        self.names = parse_names(custom.get("names")) or {0: "person"}
        self.class_ids = resolve_class_ids(settings.detect_class_list, self.names)
        self.conf_threshold = settings.conf_threshold
        self.iou_threshold = settings.iou_threshold
        version = custom.get("version")
        self.model_version = f"{path.stem}-onnx{'-ultralytics' + version if version else ''}"

    def infer(self, frame: Frame) -> FrameDetections:
        if frame.image is None:
            raise ValueError("OnnxDetector нужен кадр с изображением (не NullFrameSource)")
        t0 = time.perf_counter()
        tensor, info = letterbox(frame.image, self.input_size)
        t1 = time.perf_counter()
        (output,) = self.session.run([self.output_name], {self.input_name: tensor})
        t2 = time.perf_counter()
        detections = postprocess(
            output,
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
        self.session = None
