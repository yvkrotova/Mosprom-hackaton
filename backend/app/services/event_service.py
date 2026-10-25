"""События (эпизоды): идемпотентный upsert от конвейера и сборка ответа API."""

import csv
import io
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.errors import ConflictError, NotFoundError, UnprocessableError
from app.models import DeletedEvent, Event, OperatorAction, Source, Zone
from app.models.base import as_utc, utcnow
from app.schemas.enums import (
    REVIEW_TRANSITIONS,
    EpisodeState,
    EvidenceKind,
    OperatorActionType,
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

# Особое значение фильтра зоны: события правил «на весь кадр» (zone_id = null).
NO_ZONE = "none"
# «Передано мастеру» — подтверждённое нарушение: escalated считается подтверждённым везде
# (фильтр confirmed, статистика, CSV). Результатов проверки три: не проверено, подтверждено,
# ложное.
CONFIRMED_STATUSES = (ReviewStatus.CONFIRMED, ReviewStatus.ESCALATED)

# Каталог, который backend отдаёт как /media (видео эпизода — ссылка на исходный файл).
_media_root: Path | None = None


def configure_media(storage_dir: Path) -> None:
    global _media_root
    _media_root = Path(storage_dir).resolve()


@dataclass(frozen=True, slots=True)
class EventFilters:
    scenario_type: ScenarioType | None = None
    source_id: str | None = None
    zone_id: str | None = None  # NO_ZONE — только события без зоны
    from_: datetime | None = None
    to: datetime | None = None
    state: EpisodeState | None = None
    review_status: ReviewStatus | None = None
    priority: Priority | None = None
    site: str | None = None
    viewed: bool | None = None
    order: str = "desc"  # по началу эпизода


def interval_conditions(from_: datetime | None, to: datetime | None) -> list[ColumnElement]:
    """Период — по началу эпизода: from ≤ started_at ≤ to (одинаково в журнале и статистике,
    поэтому «Открыть в журнале» из статистики показывает те же эпизоды)."""
    conds: list[ColumnElement] = []
    if from_ is not None:
        conds.append(Event.started_at >= as_utc(from_))
    if to is not None:
        conds.append(Event.started_at <= as_utc(to))
    return conds


def filter_conditions(f: EventFilters) -> list[ColumnElement]:
    conds = interval_conditions(f.from_, f.to)
    for column, value in (
        (Event.scenario_type, f.scenario_type),
        (Event.source_id, f.source_id),
        (Event.state, f.state),
        (Event.priority, f.priority),
    ):
        if value is not None:
            conds.append(column == value)
    if f.zone_id == NO_ZONE:
        conds.append(Event.zone_id.is_(None))
    elif f.zone_id is not None:
        conds.append(Event.zone_id == f.zone_id)
    if f.review_status == ReviewStatus.CONFIRMED:
        conds.append(Event.review_status.in_(CONFIRMED_STATUSES))
    elif f.review_status is not None:
        conds.append(Event.review_status == f.review_status)
    if f.site is not None:
        conds.append(Event.source_id.in_(select(Source.id).where(Source.site == f.site)))
    if f.viewed is not None:
        conds.append(Event.viewed_at.is_not(None) if f.viewed else Event.viewed_at.is_(None))
    return conds


def _ordered(query, order: str):
    if order == "asc":
        return query.order_by(Event.started_at.asc(), Event.id)
    return query.order_by(Event.started_at.desc(), Event.id)


def list_events(session: Session, f: EventFilters, limit: int, offset: int) -> EventPage:
    conds = filter_conditions(f)
    total = session.scalar(select(func.count()).select_from(Event).where(*conds)) or 0
    events = session.scalars(
        _ordered(select(Event).where(*conds).options(selectinload(Event.evidence)), f.order)
        .limit(limit)
        .offset(offset)
    ).all()
    return EventPage(
        items=[event_to_out(session, e) for e in events], total=total, limit=limit, offset=offset
    )


def mark_viewed(session: Session, hub: WsHub, event_id: str) -> EventOut:
    """Оператор открыл событие. Идемпотентно; review_status не меняется."""
    event = get_event(session, event_id)
    if event.viewed_at is None:
        event.viewed_at = utcnow()
        session.commit()
        publish_event(session, hub, WsMessageType.EVENT_UPDATED, event)
    return event_to_out(session, event)


def call_emergency(
    session: Session,
    hub: WsHub,
    event_id: str,
    number: str,
    author: str,
    comment: str | None,
    contacts: list[dict[str, str]],
) -> EventDetail:
    """Оператор позвонил в экстренную службу по событию. Система сама не звонит: фиксирует факт
    (история действий, первый вызов в событии), поднимает приоритет до критического и рассылает
    event.emergency всем операторам. Проверку (review_status) не меняет."""
    event = get_event(session, event_id)
    contact = next((c for c in contacts if c["number"] == number), None)
    if contact is None:
        raise UnprocessableError(
            "Такой службы нет в списке EMERGENCY_CONTACTS", code="UNKNOWN_EMERGENCY_SERVICE",
            number=number, available=[c["number"] for c in contacts],
        )  # fmt: skip
    service = f"{contact['number']} — {contact['name']}"
    now = utcnow()
    if event.emergency_at is None:
        event.emergency_at, event.emergency_service, event.emergency_by = now, service, author
    event.priority = Priority.CRITICAL
    event.actions.append(
        OperatorAction(
            action=OperatorActionType.EMERGENCY,
            prev_status=event.review_status,
            new_status=event.review_status,
            comment=f"Вызов: {service}"
            + (f". {comment.strip()}" if comment and comment.strip() else ""),
            author=author,
            created_at=now,
        )
    )
    session.commit()
    publish_event(session, hub, WsMessageType.EVENT_EMERGENCY, event)
    return event_to_detail(session, event)


def view_all(session: Session, hub: WsHub, site: str | None = None) -> int:
    """«Очистить» ленту на главной: все непросмотренные события (участка) — просмотренными.
    Проверку не меняет и события не удаляет. Возвращает, сколько отмечено."""
    conds = filter_conditions(EventFilters(site=site, viewed=False))
    events = session.scalars(select(Event).where(*conds)).all()
    now = utcnow()
    for event in events:
        event.viewed_at = now
    session.commit()
    if events:
        hub.publish(WsMessageType.EVENTS_VIEWED, {"site": site, "count": len(events)})
    return len(events)


def _delete(session: Session, events: list[Event], storage_dir: Path) -> None:
    """Удаляет события с историей и кадрами (файлы тоже) и ставит «надгробия»: обновления этих
    эпизодов от идущего анализа больше не создадут их заново."""
    files = [storage_dir / ev.path for event in events for ev in event.evidence]
    now = utcnow()
    for event in events:
        session.merge(DeletedEvent(id=event.id, deleted_at=now))
        session.delete(event)  # история действий и записи доказательств — каскадом
    session.commit()
    for path in files:
        path.unlink(missing_ok=True)
    for folder in {p.parent for p in files}:
        with suppress(OSError):
            folder.rmdir()  # пустая папка события


def delete_event(session: Session, hub: WsHub, event_id: str, storage_dir: Path) -> None:
    event = get_event(session, event_id)
    _delete(session, [event], storage_dir)
    hub.publish(WsMessageType.EVENT_DELETED, {"id": event_id})


def clear_all(session: Session, hub: WsHub, storage_dir: Path) -> int:
    """«Очистить всё» в журнале: удаляет все события вместе с историей проверки и кадрами-
    доказательствами (файлы тоже). Необратимо. Идущий анализ свои старые эпизоды не вернёт
    (надгробия), новые эпизоды появятся в журнале как новые."""
    events = session.scalars(select(Event).options(selectinload(Event.evidence))).all()
    _delete(session, list(events), storage_dir)
    hub.publish(WsMessageType.EVENTS_CLEARED, {"count": len(events)})
    return len(events)


def unviewed_counts(session: Session, site: str | None = None) -> dict:
    """Непросмотренные события: всего и по камерам (участок — необязательный фильтр)."""
    conds = filter_conditions(EventFilters(site=site, viewed=False))
    rows = session.execute(
        select(Event.source_id, func.count()).where(*conds).group_by(Event.source_id)
    ).all()
    return {
        "total": sum(count for _, count in rows),
        "by_source": [{"source_id": sid, "count": count} for sid, count in rows],
    }


REVIEW_RESULT = {
    ReviewStatus.UNREVIEWED: "Не проверено",
    ReviewStatus.CONFIRMED: "Подтверждено",
    ReviewStatus.ESCALATED: "Подтверждено, передано мастеру",
    ReviewStatus.FALSE_POSITIVE: "Ложное срабатывание",
}
STATE_TEXT = {EpisodeState.ACTIVE: "Продолжается", EpisodeState.ENDED: "Завершён"}
PRIORITY_TEXT = {"low": "Низкий", "medium": "Средний", "high": "Высокий", "critical": "Критический"}
CSV_BATCH = 500


def export_csv(
    session: Session, f: EventFilters, tz_offset_min: int, scenario_titles: dict[str, str]
) -> bytes:
    """Вся выборка (не одна страница) с теми же фильтрами и сортировкой. UTF-8 с BOM, «;».
    Время — местное со смещением (смещение пояса передаёт браузер, минуты к востоку от UTC)."""
    tz = timezone(timedelta(minutes=tz_offset_min))
    sources = dict(session.execute(select(Source.id, Source.name)).all())
    zones = dict(session.execute(select(Zone.id, Zone.name)).all())
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow([
        "Начало (местное время)", "Камера", "Зона", "Нарушение", "Длительность, с",
        "Уверенность, %", "Состояние эпизода", "Результат проверки", "Приоритет",
    ])  # fmt: skip
    query = _ordered(select(Event).where(*filter_conditions(f)), f.order)
    offset = 0
    while batch := session.scalars(query.limit(CSV_BATCH).offset(offset)).all():
        for e in batch:
            start = as_utc(e.started_at).astimezone(tz)
            writer.writerow([
                start.strftime("%Y-%m-%d %H:%M:%S ") + start.strftime("%z")[:3] + ":"
                + start.strftime("%z")[3:],
                sources.get(e.source_id, "удалённая камера"),
                zones.get(e.zone_id, "удалённая зона") if e.zone_id else "Весь кадр",
                scenario_titles.get(e.scenario_type, e.scenario_type),
                f"{max(0.0, e.video_ts_end - e.video_ts_start):.1f}".replace(".", ","),
                round(e.confidence * 100),
                STATE_TEXT.get(EpisodeState(e.state), e.state),
                REVIEW_RESULT.get(ReviewStatus(e.review_status), e.review_status),
                PRIORITY_TEXT.get(e.priority, e.priority),
            ])  # fmt: skip
        offset += len(batch)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


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


def set_feedback(
    session: Session, hub: WsHub, event_id: str, verdict: str, author: str, comment: str | None
) -> tuple[EventDetail, bool]:
    """Оценка оператора в один клик. Любой статус → «подтверждено» или «ложное» одним действием
    (без промежуточного «сброса»); «передано мастеру» уже подтверждено — повторное «подтверждено»
    его не снимает. Повтор той же оценки ничего не меняет. Возвращает (событие, изменилось ли)."""
    event = get_event(session, event_id)
    current = ReviewStatus(event.review_status)
    target = ReviewStatus(verdict)
    if current == target or (target == ReviewStatus.CONFIRMED and current in CONFIRMED_STATUSES):
        return event_to_detail(session, event), False
    event.review_status = target
    event.actions.append(
        OperatorAction(
            action=(
                OperatorActionType.CONFIRM
                if target == ReviewStatus.CONFIRMED
                else OperatorActionType.MARK_FALSE
            ),
            prev_status=current,
            new_status=target,
            comment=comment,
            author=author,
        )
    )
    session.commit()
    publish_event(session, hub, WsMessageType.EVENT_REVIEWED, event)
    return event_to_detail(session, event), True


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


def episode_video(source: Source | None) -> tuple[str | None, str]:
    """Видео эпизода: ссылка на исходный файл источника (через /media) и пояснение.
    Отдельных клипов система не режет: интерфейс открывает файл на video_ts_start."""
    if source is None:
        return None, "Камера удалена — видео недоступно, есть только кадр"
    if source.kind != "file":
        return None, "Поток не записывается — доступен только кадр нарушения"
    path = Path(source.uri)
    if _media_root is None or not path.is_file():
        return None, "Файл видео недоступен на сервере — доступен только кадр нарушения"
    try:
        rel = path.resolve().relative_to(_media_root)
    except ValueError:
        return None, "Видео лежит вне каталога хранения и не отдаётся браузеру — только кадр"
    return "/media/" + rel.as_posix(), ""


def event_to_out(session: Session, event: Event) -> EventOut:
    source = session.get(Source, event.source_id)
    zone = session.get(Zone, event.zone_id) if event.zone_id else None
    columns = {c.key: getattr(event, c.key) for c in Event.__table__.columns}
    video_url, video_note = episode_video(source)
    return EventOut.model_validate(
        {
            **columns,
            "source_name": source.name if source else None,
            "source_site": source.site if source else None,
            "zone_name": zone.name if zone else None,
            "thumbnail_url": _thumbnail_url(event),
            "video_url": video_url,
            "video_note": video_note or None,
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
        if session.get(DeletedEvent, event_id) is not None:
            return UpsertOutcome.DELETED, None  # оператор удалил — не воскрешаем
        return _create(session, hub, event_id, payload)

    if event.state == EpisodeState.ENDED and payload.video_ts_end <= event.video_ts_end + 1e-6:
        # Повторный прогон той же записи (replay, детерминированный event_id) проходит уже
        # известный отрезок эпизода: ничего не меняем, тревогу повторно не шлём.
        return UpsertOutcome.UNCHANGED, event

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
