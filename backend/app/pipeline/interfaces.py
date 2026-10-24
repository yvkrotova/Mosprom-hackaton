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
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.cv import Detection, FrameDetections
from app.schemas.enums import ScenarioType

__all__ = [
    "BaseScenarioParams",
    "Detection",
    "Detector",
    "Frame",
    "FrameContext",
    "HitLevel",
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


class HitLevel(StrEnum):
    """Что правило увидело на кадре. Эпизоды собирает EpisodeManager (одинаково для всех):

    VIOLATION — нарушение есть: открывает кандидата и продолжает эпизод;
    HOLD — слабее порога начала, но достаточно, чтобы продолжить уже идущий эпизод (гистерезис);
    UNKNOWN — оценить нельзя (трек молодой, рамка у края, сцена не готова): не продолжает и не
              прерывает эпизод, но дольше gap_tolerance_s — эпизод завершается.
    Нет хита — нарушения на этом кадре нет: дольше end_window_s — эпизод завершается.
    """

    VIOLATION = "violation"
    HOLD = "hold"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class RuleHit:
    """Сигнал правила на одном кадре по одному треку (или зоне целиком, если track_id=None)."""

    scenario_type: ScenarioType
    rule_id: str
    track_id: int | None  # None — нарушение уровня зоны (например, заблокированный проход)
    zone_id: str | None
    confidence: float
    bbox: BBoxTuple | None  # рамка нарушителя; None у зонных нарушений
    video_ts: float
    label: str = "person"
    level: HitLevel = HitLevel.VIOLATION
    # Метаданные для события и кадра-доказательства (одинаковый смысл для любого сценария):
    #   details — уходит в event.details (числа: speed, blocked_pct…; EpisodeManager считает *_max);
    #   label — крупная подпись у нарушителя; trail — рисовать хвост траектории ног;
    #   shapes — фигуры [{"polygon"|"bbox": 0..1, "style": candidate|violation|info, "label"}];
    #   reason — почему UNKNOWN (для живого просмотра).
    extra: dict[str, Any] | None = None


@dataclass(slots=True)
class FrameContext:
    """Всё, что правило знает о кадре. Детектор, трекер и анализ сцены работают ОДИН раз на кадр
    в Pipeline; правила только читают результат и ничего не хранят между кадрами."""

    frame: Frame
    video_ts: float
    confirmed_tracks: list[Track]  # в правила по трекам — только подтверждённые
    all_tracks: list[Track]  # включая tentative и lost (нужно, например, чтобы маскировать людей)
    detections: FrameDetections
    scene: dict[str, Any] | None = None  # zone_id -> ZoneScene (app/pipeline/scene.py) или None


class BaseScenarioParams(BaseModel):
    """Общие параметры всех сценариев; EpisodeManager читает их одинаково для любого сценария."""

    model_config = ConfigDict(extra="forbid")

    min_confidence: float = Field(0.4, ge=0, le=1, description="Минимальная уверенность детекции")
    confirm_window_s: float = Field(
        1.0, ge=0, le=600, description="Сколько секунд видео нарушение должно держаться до тревоги"
    )
    gap_tolerance_s: float = Field(
        2.0,
        ge=0,
        le=600,
        description="Нарушение нельзя оценить (человек пропал, сцена не готова) дольше этого — "
        "эпизод завершается",
    )
    end_window_s: float | None = Field(
        None,
        ge=0,
        le=600,
        description="Нарушения явно нет дольше этого — эпизод завершается (по умолчанию = "
        "gap_tolerance_s)",
    )

    @model_validator(mode="after")
    def _default_end_window(self) -> Self:
        if self.end_window_s is None:
            self.end_window_s = self.gap_tolerance_s
        return self

    @classmethod
    def from_stored(cls, params: dict[str, Any]) -> Self:
        """Параметры из БД: поля, которых уже нет в схеме (старые версии), отбрасываются."""
        return cls.model_validate({k: v for k, v in params.items() if k in cls.model_fields})


class FrameSource(ABC):
    @abstractmethod
    def __iter__(self) -> Iterator[Frame]:
        """Кадры по возрастанию video_ts, уже прореженные до analysis_fps."""

    def close(self) -> None:  # noqa: B027 — необязательный хук
        """Освободить ресурсы (файл, поток)."""


class Detector(ABC):
    kind: ClassVar[str]  # "mock" | "replay" | "linq"
    model_version: str
    # True — inference_ms берётся из записи (replay), а не меряется этим процессом.
    recorded_timings: ClassVar[bool] = False

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
    """Сценарий нарушения (паттерн Strategy). Один экземпляр на одно правило из БД.

    Правило — ЧИСТАЯ функция кадра: по контексту, зонам и параметрам говорит, где на этом кадре
    нарушение (VIOLATION / HOLD / UNKNOWN) и с какими метаданными. Никакого состояния между кадрами:
    подтверждение, гистерезис, допуски, тревоги — в EpisodeManager, одинаково для всех сценариев.
    """

    scenario_type: ClassVar[ScenarioType]
    ParamsModel: ClassVar[type[BaseScenarioParams]]
    title: ClassVar[str]
    description: ClassVar[str]
    icon: ClassVar[str] = "alert-triangle"  # имя иконки во фронте (frontend/js/icons.js)
    color: ClassVar[str] = "#2b67ff"  # цвет зоны с этим назначением в интерфейсе
    # Поля RuleHit.extra["details"] для карточки события: ключ -> (подпись, единица). Фронт
    # показывает их одинаково для всех сценариев (значение и максимум за эпизод, ключ_max).
    details_fields: ClassVar[dict[str, tuple[str, str]]] = {}
    requires_zone: ClassVar[bool] = True
    # True — правило без зоны создаётся само для каждого источника (ensure_default_rules).
    auto_enable: ClassVar[bool] = False
    # True — правилу нужен анализ сцены (модель фона) по его зонам: Pipeline включит SceneAnalyzer.
    requires_scene_analysis: ClassVar[bool] = False

    def __init__(self, rule_id: str) -> None:
        self.rule_id = rule_id

    @abstractmethod
    def update(
        self, context: FrameContext, zones: Sequence[ZoneGeometry], params: BaseScenarioParams
    ) -> list[RuleHit]:
        """Сигналы ЭТОГО кадра. zones — зона правила; правило без зоны: все зоны источника, если
        сценарию зона обязательна, иначе пустой список (весь кадр)."""

    def describe(
        self, context: FrameContext, zones: Sequence[ZoneGeometry], params: BaseScenarioParams
    ) -> dict[int, dict[str, Any]]:
        """Необязательно: сведения о треках этого кадра для живого просмотра, например
        {track_id: {"speed": 1.2}}. Тоже без состояния."""
        return {}
