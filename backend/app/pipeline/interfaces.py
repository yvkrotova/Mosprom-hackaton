"""Интерфейсы конвейера анализа.

    FrameSource -> Detector -> Tracker -> ScenarioRule(s) -> EpisodeManager -> backend_client

Реальная модель на LinQ подключается заменой одного класса — реализации Detector.
Все длительности считаются по video_ts (секунды внутри видео), а не по часам компьютера.
Все координаты — доли кадра 0..1.
"""

from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

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
    "TrackPoint",
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


TrackState = Literal["tentative", "confirmed", "lost"]


@dataclass(slots=True, frozen=True)
class TrackPoint:
    """Точка истории трека в ПИКСЕЛЯХ исходного кадра: ноги (центр нижней кромки) и высота рамки.

    Пиксели, а не доли: скорость = смещение ног / высота рамки — отношение имеет смысл только
    в одних единицах по обеим осям.
    """

    video_ts: float
    foot_x: float
    foot_y: float
    bbox_h: float


@dataclass(slots=True)
class Track:
    track_id: int
    label: str
    bbox: BBoxTuple  # у ByteTrack — сглаженная фильтром Калмана
    confidence: float
    first_seen_ts: float
    last_seen_ts: float
    keypoints: list[tuple[float, float, float]] | None = None
    hits: int = 1
    # tentative — ещё не подтверждён (мало попаданий); lost — сейчас не виден, рамка — прогноз.
    # В правила сценариев попадают только confirmed.
    state: TrackState = "confirmed"
    # Последние ~3 с наблюдений (по сырым детекциям), общий объект на всю жизнь трека.
    history: deque[TrackPoint] = field(default_factory=lambda: deque(maxlen=128))
    frame_w: int = 0
    frame_h: int = 0


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
    # Данные сценария для события и кадра-доказательства, например {"speed": 2.4}.
    extra: dict[str, Any] | None = None


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
    # True — сценарий сам выдерживает confirm_window_s и шлёт hit только для подтверждённого
    # нарушения (с extra["start_ts"] — началом эпизода); EpisodeManager открывает эпизод сразу.
    # Нужно сценариям с гистерезисом, где «кандидат» не равен «есть hit».
    confirms_itself: ClassVar[bool] = False

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id

    def finished(self) -> list[tuple[int, str | None]]:
        """Необязательно: (track_id, zone_id) нарушений, закончившихся на последнем кадре по логике
        сценария (например, человек замедлился). EpisodeManager завершит их эпизоды сразу, не
        дожидаясь gap_tolerance_s."""
        return []

    @abstractmethod
    def update(
        self,
        tracks: Sequence[Track],
        zones: Sequence[ZoneGeometry],
        params: BaseScenarioParams,
        video_ts: float,
    ) -> list[RuleHit]:
        """zones — зона правила, либо все зоны источника, если у правила zone_id = null.
        tracks — только подтверждённые (state=confirmed) треки этого кадра."""

    def track_info(self) -> dict[int, dict[str, Any]]:
        """Необязательно: что сценарий знает о треках на последнем кадре (для живого просмотра),
        например {track_id: {"speed": 1.2, "candidate": True}}."""
        return {}
