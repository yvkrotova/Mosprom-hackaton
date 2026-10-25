"""Аналитика экрана «Статистика»: динамика по дням, рейтинг начальников смен, горячие точки.

Инцидент относится к начальнику смены, если начался во время его смены на её участке (смена без
участка — все камеры). Нарушение — инцидент, кроме отмеченных оператором ложными. Время дней —
местное (смещение пояса передаёт браузер). Реальные и демо-события считаются вместе.

Индекс безопасности (0–100) — насколько смены начальника чище средней по команде:
100 · (1 − его нарушений на смену / (2 · среднее по команде)), с ограничением 0..100.
Средний по команде начальник ≈ 50; без нарушений — 100; вдвое хуже среднего и больше — 0.
"""

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Shift, Source, Zone
from app.models.base import as_utc, utcnow
from app.schemas.enums import ReviewStatus
from app.services.event_service import CONFIRMED_STATUSES

LEADER_INDEX = 70  # «Лидер безопасности» — первое место и индекс не ниже
RISK_INDEX = 35  # «Зона риска» — индекс ниже
TREND_CHANGE = 0.3  # тренд: частота нарушений между половинами периода изменилась на ±30%…
TREND_MIN_DIFF = 0.75  # …и не меньше чем на 0,75 нарушения на смену (иначе это шум)
MIN_SHIFTS = 3  # меньше смен — в рейтинг не ставим (одна чистая смена не делает лидером)


def _period(days: int, tz: timezone, now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(tz)
    start = (local - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC), now


def _attribution(shifts: list[Shift], sites: dict[str, str | None]):
    """Функция: событие -> смена (или None)."""

    def find(event: Event) -> Shift | None:
        t = as_utc(event.started_at)
        site = sites.get(event.source_id)
        for s in shifts:
            if as_utc(s.started_at) <= t < (as_utc(s.ended_at) or utcnow()) and (
                s.site is None or s.site == site
            ):
                return s
        return None

    return find


def _risk(share: float, per_day: float) -> str:
    if share >= 0.25 or per_day >= 2:
        return "high"
    if share >= 0.1 or per_day >= 0.7:
        return "medium"
    return "low"


def overview(
    session: Session,
    days: int,
    supervisor: str | None = None,
    tz_offset_min: int = 0,
    now: datetime | None = None,
) -> dict:
    tz = timezone(timedelta(minutes=tz_offset_min))
    now = as_utc(now) or utcnow()
    start, end = _period(days, tz, now)
    prev_start = start - (end - start)

    sources = {s.id: s for s in session.scalars(select(Source))}
    sites = {sid: s.site for sid, s in sources.items()}
    zones = {z.id: z for z in session.scalars(select(Zone))}
    shifts = list(
        session.scalars(select(Shift).where(Shift.started_at < end).order_by(Shift.started_at))
    )
    shifts = [s for s in shifts if (as_utc(s.ended_at) or now) > prev_start]
    find_shift = _attribution(shifts, sites)

    all_events = list(
        session.scalars(
            select(Event).where(Event.started_at >= prev_start, Event.started_at <= end)
        )
    )
    events = [e for e in all_events if as_utc(e.started_at) >= start]
    prev_events = [e for e in all_events if as_utc(e.started_at) < start]
    shift_of = {e.id: find_shift(e) for e in events}

    def violation(e: Event) -> bool:
        return e.review_status != ReviewStatus.FALSE_POSITIVE

    # --- динамика по дням -------------------------------------------------------------------
    day_events: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        sh = shift_of[e.id]
        if supervisor and (sh is None or sh.supervisor != supervisor):
            continue
        day_events[as_utc(e.started_at).astimezone(tz).date().isoformat()].append(e)
    daily = []
    for i in range(days):
        day = (start.astimezone(tz) + timedelta(days=i)).date()
        day_start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
        day_end = day_start + timedelta(days=1)
        on_shift = []
        for s in shifts:
            overlaps = as_utc(s.started_at) < day_end and (as_utc(s.ended_at) or now) > day_start
            wanted = not supervisor or s.supervisor == supervisor
            if overlaps and wanted and s.supervisor not in on_shift:
                on_shift.append(s.supervisor)
        evs = day_events.get(day.isoformat(), [])
        by_sup = Counter(shift_of[e.id].supervisor for e in evs if shift_of[e.id] is not None)
        daily.append(
            {
                "date": day.isoformat(),
                "from": day_start.isoformat(),
                "to": min(day_end, now).isoformat(),
                "total": len(evs),
                "by_scenario": dict(Counter(e.scenario_type for e in evs)),
                "confirmed": sum(e.review_status in CONFIRMED_STATUSES for e in evs),
                "false_positive": sum(e.review_status == ReviewStatus.FALSE_POSITIVE for e in evs),
                "unreviewed": sum(e.review_status == ReviewStatus.UNREVIEWED for e in evs),
                "supervisors": on_shift,
                "by_supervisor": dict(by_sup),
            }
        )

    # --- начальники смен ---------------------------------------------------------------------
    mid = start + (end - start) / 2
    stats: dict[str, dict] = {}
    for s in shifts:
        s_start, s_end = as_utc(s.started_at), as_utc(s.ended_at) or now
        if s_end <= start:
            continue
        row = stats.setdefault(
            s.supervisor,
            {
                "supervisor": s.supervisor,
                "shifts": 0,
                "hours": 0.0,
                "incidents": 0,
                "confirmed": 0,
                "false_positive": 0,
                "unreviewed": 0,
                "active_now": False,
                "_halves": [[0, 0], [0, 0]],  # [смен, нарушений] в первой и второй половине периода
            },
        )
        row["shifts"] += 1
        row["hours"] += (min(s_end, end) - max(s_start, start)).total_seconds() / 3600
        row["active_now"] |= s.ended_at is None
        row["_halves"][0 if s_start < mid else 1][0] += 1
    for e in events:
        sh = shift_of[e.id]
        if sh is None or sh.supervisor not in stats:
            continue
        row = stats[sh.supervisor]
        if e.review_status == ReviewStatus.FALSE_POSITIVE:
            row["false_positive"] += 1
            continue
        row["incidents"] += 1
        row["confirmed"] += e.review_status in CONFIRMED_STATUSES
        row["unreviewed"] += e.review_status == ReviewStatus.UNREVIEWED
        row["_halves"][0 if as_utc(sh.started_at) < mid else 1][1] += 1
    total_shifts = sum(r["shifts"] for r in stats.values())
    team_avg = sum(r["incidents"] for r in stats.values()) / total_shifts if total_shifts else 0.0
    rows = []
    for r in stats.values():
        per_shift = r["incidents"] / r["shifts"] if r["shifts"] else 0.0
        index = (
            100.0 if team_avg == 0 else max(0.0, min(100.0, 100 * (1 - per_shift / (2 * team_avg))))
        )
        (s1, v1), (s2, v2) = r.pop("_halves")
        rate1, rate2 = (v1 / s1 if s1 else None), (v2 / s2 if s2 else None)
        if rate1 is None or rate2 is None:
            trend, change = "stable", None
        else:
            change = (rate2 - rate1) / max(rate1, 0.5)
            real = abs(rate2 - rate1) >= TREND_MIN_DIFF and abs(change) >= TREND_CHANGE
            trend = "stable" if not real else "improving" if change < 0 else "worsening"
        rows.append(
            {
                **r,
                "hours": round(r["hours"], 1),
                "per_shift": round(per_shift, 2),
                "safety_index": round(index),
                "trend": trend,
                "trend_change": None if change is None else round(change, 2),
            }
        )
    ranked = sorted(
        (r for r in rows if r["shifts"] >= MIN_SHIFTS),
        key=lambda r: (-r["safety_index"], r["per_shift"], -r["shifts"]),
    )
    few = sorted((r for r in rows if r["shifts"] < MIN_SHIFTS), key=lambda r: r["supervisor"])
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
        r["badge"] = (
            "leader"
            if i == 1 and r["safety_index"] >= LEADER_INDEX
            else "risk"
            if r["safety_index"] < RISK_INDEX
            else None
        )
    for r in few:
        r["rank"], r["badge"] = None, None  # мало смен для честного сравнения
    rows = ranked + few

    # --- горячие точки ---------------------------------------------------------------------
    violations = [e for e in events if violation(e)]
    total_v = len(violations)

    def spot_rows(key, label) -> list[dict]:
        groups: dict[str, list[Event]] = defaultdict(list)
        for e in violations:
            k = key(e)
            if k is not None:
                groups[k].append(e)
        out = []
        for k, evs in groups.items():
            share = len(evs) / total_v if total_v else 0.0
            out.append(
                {
                    **label(k),
                    "count": len(evs),
                    "share": round(share, 3),
                    "top_scenario": Counter(e.scenario_type for e in evs).most_common(1)[0][0],
                    "confirmed": sum(e.review_status in CONFIRMED_STATUSES for e in evs),
                    "risk": _risk(share, len(evs) / days),
                }
            )
        return sorted(out, key=lambda r: -r["count"])

    def zone_label(zid):
        z = zones.get(zid)
        src = sources.get(z.source_id) if z else None
        return {
            "zone_id": zid,
            "name": z.name if z else "удалённая зона",
            "source_name": src.name if src else None,
            "site": src.site if src else None,
        }

    def camera_label(sid):
        src = sources.get(sid)
        return {
            "source_id": sid,
            "name": src.name if src else "удалённая камера",
            "site": src.site if src else None,
        }

    zone_spots = spot_rows(lambda e: e.zone_id, zone_label)
    camera_spots = spot_rows(lambda e: e.source_id, camera_label)

    # --- сводка ------------------------------------------------------------------------------
    rated = [e for e in events if e.review_status != ReviewStatus.UNREVIEWED]
    confirmed = sum(e.review_status in CONFIRMED_STATUSES for e in events)
    prev_total = len(prev_events)
    summary = {
        "total": len(events),
        "violations": total_v,
        "prev_total": prev_total,
        "change": round((len(events) - prev_total) / prev_total, 3) if prev_total else None,
        "top_zone": zone_spots[0] if zone_spots else None,
        "best_supervisor": ranked[0] if ranked else None,
        "confirmed": confirmed,
        "rated": len(rated),
        "confirmed_share": round(confirmed / len(rated), 3) if rated else None,
        "demo": any(e.model_version == "demo-history" for e in events),
    }
    return {
        "days": days,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "supervisor": supervisor,
        "summary": summary,
        "daily": daily,
        "supervisors": rows,
        "hotspots": {"zones": zone_spots, "cameras": camera_spots},
    }
