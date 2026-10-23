"""Живой просмотр: кадр источника с треками, позами и состоянием относительно зон.

Воркер шлёт POST /internal/live/{source_id}; фронт получает WS /ws/live/{source_id}.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import BBox, InputModel
from app.schemas.cv import Keypoint


class LiveTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: int
    label: str
    confidence: float = Field(ge=0, le=1)
    bbox: BBox
    keypoints: list[Keypoint] | None = None
    # normal — вне зон; in_zone — нарушает, но ещё не подтверждено; alert — идёт тревога.
    state: Literal["normal", "in_zone", "alert"] = "normal"
    zone_ids: list[str] = Field(default_factory=list)


class LiveFrameMeta(InputModel):
    frame_idx: int = Field(ge=0)
    video_ts: float = Field(ge=0)
    width: int = Field(gt=0, description="Размер исходного кадра")
    height: int = Field(gt=0)
    fps: float | None = None
    inference_ms: float | None = None
    tracks: list[LiveTrack] = Field(default_factory=list)
