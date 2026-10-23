"""Интерфейсы конвейера анализа.

    FrameSource -> Detector -> Tracker -> ScenarioRule(s) -> EpisodeManager -> backend_client

Реальная модель на LinQ подключается заменой одного класса — реализации Detector.
Все длительности считаются по video_ts (секунды внутри видео), а не по часам компьютера.
Все координаты — доли кадра 0..1.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cv import Detection, FrameDetections
from app.schemas.enums import ScenarioType

__all__ = [
    "BaseScenarioParams",
    "Detection",
    "Detector",
    "Frame",
    "FrameDetections",
    "FrameSource",
    "RuleHit",
    "ScenarioRule",
    "Track",
    "Tracker",
    "ZoneGeometry",
]

BBoxTuple = tuple[float, float, float, float]


@dataclass(slots=True)
class Frame:
    source_id: str
    frame_idx: int
    video_ts: float
    width: int
    height: int
    image: np.ndarray | None = None  # BGR (как отдаёт OpenCV); None у NullFrameSource


@dataclass(slots=True)
class Track:
    track_id: int
    label: str
    bbox: BBoxTuple
    confidence: float
    first_seen_ts: float
    last_seen_ts: float
    keypoints: list[tuple[float, float, float]] | None = None


@dataclass(slots=True, frozen=True)
class ZoneGeometry:
    id: str
    name: str
    polygon: list[tuple[float, float]]


@dataclass(slots=True, frozen=True)
class RuleHit:
    """Нарушение на одном кадре. Эпизоды из хитов собирает EpisodeManager."""

    scenario_type: ScenarioType
    rule_id: str
    track_id: int
    zone_id: str | None
    confidence: float
    bbox: BBoxTuple
    video_ts: float
    label: str = "person"


class BaseScenarioParams(BaseModel):
    """Общие параметры всех сценариев; EpisodeManager читает их одинаково для любого сценария."""

    model_config = ConfigDict(extra="forbid")

    min_confidence: float = Field(0.4, ge=0, le=1, description="Минимальная уверенность детекции")
    confirm_window_s: float = Field(
        1.0, ge=0, le=60, description="Сколько секунд видео нарушение должно держаться до тревоги"
    )
    gap_tolerance_s: float = Field(
        2.0, ge=0, le=60, description="Пропуск наблюдения короче этого не завершает эпизод"
    )


class FrameSource(ABC):
    @abstractmethod
    def __iter__(self) -> Iterator[Frame]:
        """Кадры по возрастанию video_ts, уже прореженные до analysis_fps."""

    def close(self) -> None:  # noqa: B027 — необязательный хук
        """Освободить ресурсы (файл, поток)."""


class Detector(ABC):
    kind: ClassVar[str]  # "mock" | "replay" | "linq"
    model_version: str

    @abstractmethod
    def infer(self, frame: Frame) -> FrameDetections:
        """Детекции для кадра. Ошибка — исключение, а не пустой список."""

    def close(self) -> None:  # noqa: B027 — необязательный хук
        """Освободить ускоритель/файлы."""


class Tracker(ABC):
    @abstractmethod
    def update(self, dets: FrameDetections) -> list[Track]:
        """Треки, наблюдаемые на этом кадре."""


class ScenarioRule(ABC):
    """Сценарий нарушения. Один экземпляр на одно правило из БД.

    Правило не знает про эпизоды и тревоги: на каждом кадре оно только говорит,
    какие треки сейчас нарушают. Подтверждение, допуск потери и завершение — в EpisodeManager.
    """

    scenario_type: ClassVar[ScenarioType]
    ParamsModel: ClassVar[type[BaseScenarioParams]]
    title: ClassVar[str]
    description: ClassVar[str]
    requires_zone: ClassVar[bool] = True

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id

    @abstractmethod
    def update(
        self,
        tracks: Sequence[Track],
        zones: Sequence[ZoneGeometry],
        params: BaseScenarioParams,
        video_ts: float,
    ) -> list[RuleHit]:
        """zones — зона правила, либо все зоны источника, если у правила zone_id = null."""
