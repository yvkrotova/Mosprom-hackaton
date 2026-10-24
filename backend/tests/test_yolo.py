"""letterbox и postprocess YOLOv8 на синтетических данных; OnnxDetector — если есть файл модели."""

import numpy as np
import pytest

from app.config import BACKEND_DIR, Settings
from app.pipeline.detectors.yolo_common import (
    LetterboxInfo,
    letterbox,
    parse_names,
    postprocess,
    resolve_class_ids,
)
from app.pipeline.interfaces import Frame

NAMES = {0: "person", 2: "car"}
MODEL = BACKEND_DIR / "models" / "yolov8s.onnx"


@pytest.mark.parametrize(
    ("h", "w", "scale", "pad"),
    [
        (720, 1280, 0.5, (0, 140)),  # широкий кадр: поля сверху и снизу
        (1280, 720, 0.5, (140, 0)),  # вертикальный: поля слева и справа
        (640, 640, 1.0, (0, 0)),  # квадрат без полей
        (240, 320, 2.0, (0, 80)),  # маленький кадр увеличивается
    ],
)
def test_letterbox_shapes_and_padding(h, w, scale, pad):
    image = np.zeros((h, w, 3), np.uint8)
    image[..., 0] = 255  # синий канал BGR
    tensor, info = letterbox(image, 640)
    assert tensor.shape == (1, 3, 640, 640) and tensor.dtype == np.float32
    assert info == LetterboxInfo(scale, pad[0], pad[1], w, h)
    # Центр — картинка: BGR -> RGB, синий оказался в канале 2, значения 0..1.
    assert tensor[0, :, 320, 320].tolist() == [0.0, 0.0, 1.0]
    # Угол — паддинг 114, если поля есть.
    if pad != (0, 0):
        assert tensor[0, :, 0, 0] == pytest.approx([114 / 255] * 3)


def _output(*boxes):
    """boxes: (cx, cy, w, h, class_id, score) в пикселях входа 640 -> тензор [1, 84, 8400]."""
    out = np.zeros((1, 84, 8400), np.float32)
    for i, (cx, cy, bw, bh, cls, score) in enumerate(boxes):
        out[0, :4, i] = (cx, cy, bw, bh)
        out[0, 4 + cls, i] = score
    return out


INFO_720P = LetterboxInfo(scale=0.5, pad_x=0, pad_y=140, frame_w=1280, frame_h=720)


def test_postprocess_unletterbox_threshold_nms():
    out = _output(
        (400, 320, 160, 180, 0, 0.9),  # человек
        (405, 322, 160, 180, 0, 0.8),  # дубль того же человека -> NMS
        (100, 300, 50, 100, 0, 0.2),  # ниже порога
        (400, 320, 160, 180, 2, 0.7),  # машина на том же месте: другой класс, NMS не трогает
    )
    dets = postprocess(out, INFO_720P, NAMES, conf_threshold=0.35, iou_threshold=0.45)
    assert [(d.label, d.confidence) for d in dets] == [("person", 0.9), ("car", 0.7)]
    # (320..480, 230..410) во входе -> минус паддинг 140 по y, / 0.5 -> / кадр.
    assert dets[0].bbox == pytest.approx((0.5, 0.25, 0.75, 0.75))


def test_postprocess_class_filter_and_clipping():
    out = _output(
        (400, 320, 160, 180, 2, 0.9),  # машина — отфильтрована
        (20, 320, 100, 100, 0, 0.6),  # человек у левого края: x1 < 0 -> обрезка
    )
    dets = postprocess(out, INFO_720P, NAMES, class_ids={0})
    assert len(dets) == 1 and dets[0].label == "person"
    x1, y1, x2, y2 = dets[0].bbox
    assert x1 == 0.0 and x2 == pytest.approx(70 / 0.5 / 1280, abs=1e-4)
    assert (y1, y2) == pytest.approx(((270 - 140) / 0.5 / 720, (370 - 140) / 0.5 / 720), abs=1e-4)


def test_postprocess_empty_and_bad_shape():
    assert postprocess(_output(), INFO_720P, NAMES) == []
    with pytest.raises(ValueError, match="форма"):
        postprocess(np.zeros((1, 3, 10), np.float32), INFO_720P, NAMES)


def test_names_and_classes():
    names = parse_names("{0: 'person', 2: 'car'}")
    assert names == NAMES
    assert resolve_class_ids(["person"], names) == {0}
    assert resolve_class_ids([], names) is None
    with pytest.raises(ValueError, match="dog"):
        resolve_class_ids(["dog"], names)


@pytest.mark.skipif(not MODEL.is_file(), reason="нет backend/models/yolov8s.onnx")
def test_onnx_detector_runs_on_real_model():
    pytest.importorskip("onnxruntime")
    from app.pipeline.detectors.onnx import OnnxDetector

    detector = OnnxDetector(Settings(_env_file=None, model_path=str(MODEL)))
    assert detector.kind == "onnx_cpu" and detector.input_size == 640
    assert detector.names[0] == "person" and detector.class_ids == {0}
    image = np.full((480, 640, 3), 90, np.uint8)
    dets = detector.infer(Frame("s", 7, 0.7, 640, 480, image))
    assert dets.frame_idx == 7 and dets.inference_ms > 0
    assert dets.preprocess_ms is not None and dets.postprocess_ms is not None
    assert dets.model_version.startswith("yolov8s-onnx")
    assert all(d.label == "person" for d in dets.detections)
    detector.close()
