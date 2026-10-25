"""Смены руководителей и рейтинг безопасности.

Опасная ситуация смены — эпизод, начавшийся во время смены на её участке (смена без участка
отвечает за все камеры), кроме отмеченных оператором ложными. Рейтинг — опасных ситуаций на
8 часов смены: меньше — выше место (смены разной длины сравниваются честно).
"""

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.errors import ConflictError, NotFoundError, UnprocessableError
from app.models import Event, Shift, Source
from app.models.base import as_utc, utcnow
from app.schemas.enums import ReviewStatus
from app.schemas.shifts import ShiftCreate, ShiftUpdate
from app.services.event_service import CONFIRMED_STATUSES

NORM_HOURS = 8.0  # «на смену»: опасных ситуаций на 8 часов


def get_shift(session: Session, shift_id: str) -> Shift:
    shift = session.get(Shift, shift_id)
    if shift is None:
        raise NotFoundError("Смена не найдена", code="SHIFT_NOT_FOUND", shift_id=shift_id)
    return shift


def list_shifts(session: Session, from_: datetime | None, to: datetime | None) -> list[Shift]:
    """Смены, пересекающиеся с периодом (идущие — до сих пор)."""
    q = select(Shift).order_by(Shift.started_at.desc())
    if to is not None:
        q = q.where(Shift.started_at <= as_utc(to))
    if from_ is not None:
        q = q.where(or_(Shift.ended_at.is_(None), Shift.ended_at >= as_utc(from_)))
    return list(session.scalars(q))


def _check(session: Session, supervisor: str, site: str | None, start: datetime,
           end: datetime | None, exclude_id: str | None = None) -> None:  # fmt: skip
    if not supervisor.strip():
        raise UnprocessableError("Укажите руководителя смены", code="SHIFT_SUPERVISOR_REQUIRED")
    if end is not None and end <= start:
        raise UnprocessableError("Конец смены должен быть позже начала", code="SHIFT_BAD_INTERVAL")
    # На участке в одно время — один ответственный («все участки» пересекается со всеми).
    q = select(Shift).where(
        or_(Shift.ended_at.is_(None), Shift.ended_at > start),
        *([Shift.started_at < end] if end is not None else []),
    )
    if exclude_id:
        q = q.where(Shift.id != exclude_id)
    for other in session.scalars(q):
        if other.site is None or site is None or other.site == site:
            raise ConflictError(
                f"В это время за {('участок «' + site + '»') if site else 'все участки'} уже "
                f"отвечает {other.supervisor}",
                code="SHIFT_OVERLAP",
                shift_id=other.id,
            )


def create_shift(session: Session, body: ShiftCreate) -> Shift:
    start = as_utc(body.started_at) or utcnow()
    end = as_utc(body.ended_at)
    site = (body.site or "").strip() or None
    _check(session, body.supervisor, site, start, end)
    shift = Shift(supervisor=body.supervisor.strip(), site=site, started_at=start, ended_at=end)
    session.add(shift)
    session.commit()
    return shift


def update_shift(session: Session, shift_id: str, body: ShiftUpdate) -> Shift:
    shift = get_shift(session, shift_id)
    changes = body.model_dump(exclude_unset=True)
    supervisor = (changes.get("supervisor") or shift.supervisor).strip()
    site = (changes["site"] or "").strip() or None if "site" in changes else shift.site
    start = as_utc(changes.get("started_at")) or as_utc(shift.started_at)
    end = as_utc(changes["ended_at"]) if "ended_at" in changes else as_utc(shift.ended_at)
    _check(session, supervisor, site, start, end, exclude_id=shift.id)
    shift.supervisor, shift.site, shift.started_at, shift.ended_at = supervisor, site, start, end
    session.commit()
    return shift


def end_shift(session: Session, shift_id: str) -> Shift:
    shift = get_shift(session, shift_id)
    if shift.ended_at is None:
        shift.ended_at = max(utcnow(), as_utc(shift.started_at) + timedelta(seconds=1))
        session.commit()
    return shift


def delete_shift(session: Session, shift_id: str) -> None:
    session.delete(get_shift(session, shift_id))
    session.commit()


def rating(
    session: Session, from_: datetime | None, to: datetime | None, site: str | None = None
) -> list[dict]:
    """Рейтинг руководителей за период: по каждой — часы смен, опасные ситуации и их частота."""
    now = utcnow()
    period_from, period_to = as_utc(from_), min(as_utc(to) or now, now)
    sites = dict(session.execute(select(Source.id, Source.site)).all())
    rows: dict[str, dict] = {}
    for shift in list_shifts(session, from_, to):
        if site and shift.site not in (None, site):
            continue
        start = (
            max(as_utc(shift.started_at), period_from) if period_from else as_utc(shift.started_at)
        )
        end = min(as_utc(shift.ended_at) or now, period_to)
        if end <= start:
            continue
        row = rows.setdefault(shift.supervisor, {
            "supervisor": shift.supervisor, "shifts": 0, "hours": 0.0, "incidents": 0,
            "confirmed": 0, "unreviewed": 0, "false_positive": 0, "active_now": False,
        })  # fmt: skip
        row["shifts"] += 1
        row["hours"] += (end - start).total_seconds() / 3600
        row["active_now"] |= shift.ended_at is None
        events = session.scalars(
            select(Event).where(Event.started_at >= start, Event.started_at < end)
        ).all()
        for ev in events:
            ev_site = sites.get(ev.source_id)
            if shift.site is not None and ev_site != shift.site:
                continue
            if site and ev_site != site:
                continue
            if ev.review_status == ReviewStatus.FALSE_POSITIVE:
                row["false_positive"] += 1
                continue
            row["incidents"] += 1
            if ev.review_status in CONFIRMED_STATUSES:
                row["confirmed"] += 1
            else:
                row["unreviewed"] += 1
    out = []
    for row in rows.values():
        hours = row["hours"]
        row["hours"] = round(hours, 2)
        row["per_shift"] = round(row["incidents"] / hours * NORM_HOURS, 2) if hours > 0 else None
        out.append(row)
    out.sort(key=lambda r: (r["per_shift"] if r["per_shift"] is not None else 1e9, -r["hours"]))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out
