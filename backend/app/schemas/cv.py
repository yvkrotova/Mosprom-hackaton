"""CV-контракт: что детектор (mock, replay-файл, LinQ) отдаёт конвейеру на каждый кадр.

Одна строка JSONL-файла для DETECTOR=replay = один объект FrameDetections.
Подробности и примеры — docs/CV_CONTRACT.md.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import BBox, RelCoord

Keypoint = tuple[RelCoord, RelCoord, float]  # x, y в долях кадра; conf 0..1


class Detection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, examples=["person"])
    confidence: float = Field(ge=0, le=1)
    bbox: BBox = Field(description="[x1, y1, x2, y2] в долях кадра 0..1")
    track_id: int | None = Field(None, description="Если модель сама трекает; иначе трекер backend")
    keypoints: list[Keypoint] | None = Field(None, description="Для поз (fall_immobility)")


class FrameDetections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str | None = Field(
        None, description="Можно не указывать в replay-файле: конвейер подставит свой source_id"
    )
    frame_idx: int = Field(ge=0, description="Номер кадра в исходном видео (с нуля)")
    video_ts: float = Field(ge=0, description="Время кадра внутри видео, секунды")
    frame_w: int = Field(gt=0, description="Ширина исходного кадра, пиксели")
    frame_h: int = Field(gt=0)
    inference_ms: float = Field(ge=0, description="Чистое время инференса кадра, мс")
    preprocess_ms: float | None = Field(None, ge=0, description="Подготовка входа сети, мс")
    postprocess_ms: float | None = Field(None, ge=0, description="Декодирование выхода и NMS, мс")
    device_ms: float | None = Field(
        None,
        ge=0,
        description="Время на самом ускорителе, если API его отдаёт (сеть = inference − device)",
    )
    model_version: str = Field(min_length=1, examples=["yolov8n-linq-int8-2026.10"])
    detector_kind: str | None = Field(
        None,
        description="Чем посчитано: onnx_cpu, linq_tpu… Replay файла linq_tpu — запись с LinQ",
    )
    detections: list[Detection] = Field(default_factory=list)
    # False — кадр не проанализирован (например, в предзаписанном файле нет такого кадра):
    # правила и эпизоды трактуют его как «оценить нельзя», а не «людей нет».
    analyzed: bool = True
    unanalyzed_reason: str | None = None
