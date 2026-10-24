"""События (эпизоды): идемпотентный upsert от конвейера и сборка ответа API."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.errors import ConflictError, NotFoundError
from app.models import Event, OperatorAction, Source, Zone
from app.models.base import as_utc, utcnow
from app.schemas.enums import (
    REVIEW_TRANSITIONS,
    EpisodeState,
    EvidenceKind,
    Priority,
    ReviewStatus,
    ScenarioType,
    UpsertOutcome,
    WsMessageType,
)
from app.schemas.events import (
    EventDetail,
    EventOut,
    EventPage,
    EvidenceOut,
    OperatorActionCreate,
    OperatorActionOut,
)
from app.schemas.internal import EventUpsert
from app.services.notifier import WsHub


@dataclass(frozen=True, slots=True)
class EventFilters:
    scenario_type: ScenarioType | None = None
    source_id: str | None = None
    zone_id: str | None = None
    from_: datetime | None = None
    to: datetime | None = None
    state: EpisodeState | None = None
    review_status: ReviewStatus | None = None
    priority: Priority | None = None


def interval_conditions(from_: datetime | None, to: datetime | None) -> list[ColumnElement]:
    """Эпизод пересекается с [from, to]. Активный эпизод (ended_at = null) длится до сих пор."""
    conds: list[ColumnElement] = []
    if to is not None:
        conds.append(Event.started_at <= as_utc(to))
    if from_ is not None:
        conds.append(or_(Event.ended_at.is_(None), Event.ended_at >= as_utc(from_)))
    return conds


def filter_conditions(f: EventFilters) -> list[ColumnElement]:
    conds = interval_conditions(f.from_, f.to)
    for column, value in (
        (Event.scenario_type, f.scenario_type),
        (Event.source_id, f.source_id),
        (Event.zone_id, f.zone_id),
        (Event.state, f.state),
        (Event.review_status, f.review_status),
        (Event.priority, f.priority),
    ):
        if value is not None:
            conds.append(column == value)
    return conds


def list_events(session: Session, f: EventFilters, limit: int, offset: int) -> EventPage:
    conds = filter_conditions(f)
    total = session.scalar(select(func.count()).select_from(Event).where(*conds)) or 0
    events = session.scalars(
        select(Event)
        .where(*conds)
        .options(selectinload(Event.evidence))
        .order_by(Event.started_at.desc(), Event.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return EventPage(
        items=[event_to_out(session, e) for e in events], total=total, limit=limit, offset=offset
    )


def end_active_events(session: Session, hub: WsHub, source_id: str) -> int:
    """Закрывает эпизоды, которые воркер не успел завершить (упал или был убит)."""
    events = session.scalars(
        select(Event).where(Event.source_id == source_id, Event.state == EpisodeState.ACTIVE)
    ).all()
    for event in events:
        event.state = EpisodeState.ENDED
        event.ended_at = event.last_seen_at
    session.commit()
    for event in events:
        publish_event(session, hub, WsMessageType.EVENT_ENDED, event)
    return len(events)


def apply_action(
    session: Session, hub: WsHub, event_id: str, body: OperatorActionCreate
) -> EventDetail:
    """Действие оператора: меняет только review_status (по таблице переходов) и пишет историю."""
    event = get_event(session, event_id)
    current = ReviewStatus(event.review_status)
    allowed_from, target = REVIEW_TRANSITIONS[body.action]
    if current not in allowed_from:
        raise ConflictError(
            f"Действие «{body.action}» недоступно для статуса «{current}»",
            code="INVALID_TRANSITION",
            action=body.action,
            current_status=current,
            allowed_from=sorted(allowed_from),
        )
    new_status = target or current
    event.review_status = new_status
    event.actions.append(
        OperatorAction(
            action=body.action,
            prev_status=current,
            new_status=new_status,
            comment=body.comment,
            author=body.author,
        )
    )
    session.commit()
    publish_event(session, hub, WsMessageType.EVENT_REVIEWED, event)
    return event_to_detail(session, event)


def get_event(session: Session, event_id: str) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise NotFoundError("Событие не найдено", code="EVENT_NOT_FOUND", event_id=event_id)
    return event


def _thumbnail_url(event: Event) -> str | None:
    for kind in (EvidenceKind.FRAME_ANNOTATED, EvidenceKind.FRAME_RAW):
        for ev in event.evidence:
            if ev.kind == kind:
                return ev.url
    return None


def event_to_out(session: Session, event: Event) -> EventOut:
    source = session.get(Source, event.source_id)
    zone = session.get(Zone, event.zone_id) if event.zone_id else None
    columns = {c.key: getattr(event, c.key) for c in Event.__table__.columns}
    return EventOut.model_validate(
        {
            **columns,
            "source_name": source.name if source else None,
            "zone_name": zone.name if zone else None,
            "thumbnail_url": _thumbnail_url(event),
            "evidence": [EvidenceOut.model_validate(e) for e in event.evidence],
        }
    )


def event_to_detail(session: Session, event: Event) -> EventDetail:
    return EventDetail(
        **event_to_out(session, event).model_dump(),
        actions=[OperatorActionOut.model_validate(a) for a in event.actions],
    )


def publish_event(session: Session, hub: WsHub, msg_type: WsMessageType, event: Event) -> None:
    hub.publish(msg_type, event_to_out(session, event).model_dump(mode="json"))


def _same(old: object, new: object) -> bool:
    if isinstance(old, datetime) or isinstance(new, datetime):
        return as_utc(old) == as_utc(new)  # type: ignore[arg-type]
    return old == new


def upsert_event(
    session: Session, hub: WsHub, event_id: str, payload: EventUpsert
) -> tuple[UpsertOutcome, Event]:
    """Создаёт или обновляет эпизод. Повтор того же тела ничего не меняет и не шлёт в WS."""
    event = session.get(Event, event_id)
    if event is None:
        return _create(session, hub, event_id, payload)

    if event.state == EpisodeState.ENDED and payload.state == EpisodeState.ACTIVE:
        raise ConflictError(
            "Эпизод уже завершён; продолжение нарушения — это новый эпизод с новым event_id",
            code="EPISODE_ALREADY_ENDED",
            event_id=event_id,
        )

    # Меняются только поля, которые растут по ходу эпизода. review_status не трогаем никогда.
    wanted = {
        "last_seen_at": payload.last_seen_at,
        "video_ts_end": payload.video_ts_end,
        "confidence": max(event.confidence, payload.confidence),
        "state": payload.state,
        "ended_at": payload.ended_at,
        "description": payload.description,
    }
    if payload.details is not None:
        wanted["details"] = payload.details
    changed = {k: v for k, v in wanted.items() if not _same(getattr(event, k), v)}
    if not changed:
        return UpsertOutcome.UNCHANGED, event

    ending = event.state != EpisodeState.ENDED and payload.state == EpisodeState.ENDED
    for key, value in changed.items():
        setattr(event, key, value)
    session.commit()

    if ending:
        publish_event(session, hub, WsMessageType.EVENT_ENDED, event)
        return UpsertOutcome.ENDED, event
    publish_event(session, hub, WsMessageType.EVENT_UPDATED, event)
    return UpsertOutcome.UPDATED, event


def _create(
    session: Session, hub: WsHub, event_id: str, payload: EventUpsert
) -> tuple[UpsertOutcome, Event]:
    if session.get(Source, payload.source_id) is None:
        raise NotFoundError(
            "Источник не найден", code="SOURCE_NOT_FOUND", source_id=payload.source_id
        )

    now = utcnow()
    event = Event(id=event_id, **payload.model_dump(), received_at=now)
    # Тревога = event.created. notified_at фиксируем в момент отправки в WS.
    event.notified_at = now
    event.alerts_sent = 1
    session.add(event)
    try:
        session.commit()
    except IntegrityError:
        # Параллельный повтор того же PUT успел создать запись — обрабатываем как обновление.
        session.rollback()
        return upsert_event(session, hub, event_id, payload)

    publish_event(session, hub, WsMessageType.EVENT_CREATED, event)
    return UpsertOutcome.CREATED, event
