"""Перечисления предметной области и таблица переходов статуса проверки."""

from enum import StrEnum


class ScenarioType(StrEnum):
    DANGER_ZONE = "danger_zone"
    FALL_IMMOBILITY = "fall_immobility"  # пока только в enum и шаблоне, без реализации
    RUN_FAST_MOVEMENT = "run_fast_movement"  # бег / резкое перемещение


class EpisodeState(StrEnum):
    """Состояние эпизода. Меняет только конвейер."""

    ACTIVE = "active"
    ENDED = "ended"


class ReviewStatus(StrEnum):
    """Результат проверки. Меняет только оператор."""

    UNREVIEWED = "unreviewed"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    ESCALATED = "escalated"


class OperatorActionType(StrEnum):
    CONFIRM = "confirm"
    MARK_FALSE = "mark_false"
    ESCALATE = "escalate"
    COMMENT = "comment"
    RESET = "reset"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SourceKind(StrEnum):
    FILE = "file"
    RTSP = "rtsp"
    IMAGES = "images"


class SourceStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class EvidenceKind(StrEnum):
    FRAME_RAW = "frame_raw"
    FRAME_ANNOTATED = "frame_annotated"
    CLIP = "clip"


class WsMessageType(StrEnum):
    EVENT_CREATED = "event.created"
    EVENT_UPDATED = "event.updated"
    EVENT_ENDED = "event.ended"
    EVENT_REVIEWED = "event.reviewed"
    SOURCE_STATUS = "source.status"


class UpsertOutcome(StrEnum):
    """Результат PUT /internal/events/{id}."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    ENDED = "ended"


# Действие оператора -> (из каких статусов разрешено, в какой статус переводит).
# None в качестве целевого статуса значит «статус не меняется» (comment).
Transition = tuple[frozenset[ReviewStatus], ReviewStatus | None]

REVIEW_TRANSITIONS: dict[OperatorActionType, Transition] = {
    OperatorActionType.CONFIRM: (
        frozenset({ReviewStatus.UNREVIEWED, ReviewStatus.ESCALATED}),
        ReviewStatus.CONFIRMED,
    ),
    OperatorActionType.MARK_FALSE: (
        frozenset({ReviewStatus.UNREVIEWED, ReviewStatus.ESCALATED, ReviewStatus.CONFIRMED}),
        ReviewStatus.FALSE_POSITIVE,
    ),
    OperatorActionType.ESCALATE: (
        frozenset({ReviewStatus.UNREVIEWED, ReviewStatus.CONFIRMED}),
        ReviewStatus.ESCALATED,
    ),
    OperatorActionType.RESET: (
        frozenset({ReviewStatus.CONFIRMED, ReviewStatus.FALSE_POSITIVE, ReviewStatus.ESCALATED}),
        ReviewStatus.UNREVIEWED,
    ),
    OperatorActionType.COMMENT: (frozenset(ReviewStatus), None),
}
