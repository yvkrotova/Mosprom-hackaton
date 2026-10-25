"""Демо-история для экрана «Статистика»: начальники смен, смены и инциденты за N дней.

Данные помечены (`Source.is_demo`, `Shift.is_demo`, `Event.model_version = "demo-history"`):
в аналитике они идут вместе с реальными событиями, а `DELETE /demo/history` убирает их целиком.
Паттерны заложены специально, чтобы рейтинг было что показывать:
- Иванов И.И. — отличная дисциплина (почти без нарушений);
- Петров А.С. — антилидер (стабильно много нарушений);
- Сидоров В.М. — положительный тренд (к концу периода нарушений всё меньше);
- Кузнецов Д.О., Смирнова Е.А. — средние стабильные показатели.
"""

import random
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Event, Rule, Shift, Source, Zone
from app.models.base import as_utc, utcnow
from app.schemas.enums import EpisodeState, Priority, ReviewStatus, ScenarioType, SourceKind

DEMO_MODEL = "demo-history"
SHIFT_HOURS = 12  # день 08:00–20:00 и ночь 20:00–08:00 (местное время)

# (ФИО, среднее нарушений за смену в начале периода, в конце периода)
SUPERVISORS = [
    ("Иванов И.И.", 0.5, 0.5),
    ("Петров А.С.", 6.5, 6.5),
    ("Сидоров В.М.", 6.0, 1.0),
    ("Кузнецов Д.О.", 3.0, 3.0),
    ("Смирнова Е.А.", 2.5, 2.5),
]

# Демо-камеры: (название, участок, вес «опасности», зоны: (название, сценарий, полигон))
CAMERAS = [
    (
        "Демо · Камера 01",
        "Склад",
        0.42,
        [
            (
                "Проезд погрузчика",
                ScenarioType.DANGER_ZONE,
                [[0.1, 0.5], [0.6, 0.5], [0.6, 0.95], [0.1, 0.95]],
            ),
            (
                "Стеллажи ряд B",
                ScenarioType.DANGER_ZONE,
                [[0.62, 0.3], [0.95, 0.3], [0.95, 0.9], [0.62, 0.9]],
            ),
        ],
    ),
    (
        "Демо · Камера 02",
        "Цех №2",
        0.28,
        [
            (
                "Пресс П-12",
                ScenarioType.DANGER_ZONE,
                [[0.3, 0.4], [0.7, 0.4], [0.7, 0.9], [0.3, 0.9]],
            ),
        ],
    ),
    (
        "Демо · Камера 03",
        "Погрузка",
        0.2,
        [
            (
                "Рампа 3",
                ScenarioType.DANGER_ZONE,
                [[0.2, 0.55], [0.8, 0.55], [0.8, 0.95], [0.2, 0.95]],
            ),
        ],
    ),
    (
        "Демо · Камера 04",
        "Выход",
        0.1,
        [
            (
                "Эвакуационный выход",
                ScenarioType.BLOCKED_EXIT,
                [[0.35, 0.5], [0.65, 0.5], [0.65, 0.95], [0.35, 0.95]],
            ),
        ],
    ),
]

SCENARIO_WEIGHTS = {
    ScenarioType.DANGER_ZONE: 0.55,
    ScenarioType.RUN_FAST_MOVEMENT: 0.3,
    ScenarioType.BLOCKED_EXIT: 0.15,
}


def _count(rng: random.Random, mean: float) -> int:
    """Число инцидентов за смену: среднее профиля с умеренным разбросом (±25%). Пуассон на 12 сменах
    слишком шумит — заложенные паттерны (тренд, антилидер) тонули бы в случайности."""
    return max(0, round(rng.gauss(mean, 0.25 * mean + 0.3)))


def _demo_sources(session: Session) -> list[tuple[Source, list[Zone]]]:
    out = []
    for name, site, _weight, zones in CAMERAS:
        source = session.scalar(select(Source).where(Source.name == name, Source.is_demo.is_(True)))
        if source is None:
            source = Source(
                name=name,
                kind=SourceKind.RTSP,
                uri=f"demo://{name}",
                site=site,
                analysis_fps=10.0,
                is_demo=True,
                defaults_applied=["run_fast_movement"],
            )
            session.add(source)
            session.flush()
            for zname, scenario, polygon in zones:
                zone = Zone(source_id=source.id, name=zname, polygon=polygon)
                session.add(zone)
                session.flush()
                session.add(
                    Rule(
                        source_id=source.id,
                        zone_id=zone.id,
                        scenario_type=scenario,
                        enabled=True,
                        priority=Priority.HIGH,
                        params={},
                        version=1,
                    )
                )
        zone_rows = list(session.scalars(select(Zone).where(Zone.source_id == source.id)))
        out.append((source, zone_rows))
    return out


def clear(session: Session) -> dict[str, int]:
    """Удаляет только демо-данные: события demo-history, демо-смены и демо-камеры."""
    events = session.execute(delete(Event).where(Event.model_version == DEMO_MODEL)).rowcount
    shifts = session.execute(delete(Shift).where(Shift.is_demo.is_(True))).rowcount
    sources = list(session.scalars(select(Source).where(Source.is_demo.is_(True))))
    for source in sources:
        session.delete(source)  # зоны и правила — каскадом
    session.commit()
    return {"events": events or 0, "shifts": shifts or 0, "sources": len(sources)}


def generate(
    session: Session,
    days: int = 30,
    tz_offset_min: int = 180,
    seed: int = 2026,
    now: datetime | None = None,
) -> dict[str, int]:
    """Пересоздаёт демо-историю за `days` дней до текущего момента (старая демо-история удаляется).
    Реальные смены не трогаются: демо-смены, пересекающиеся с ними, пропускаются."""
    clear(session)
    rng = random.Random(seed)
    now = as_utc(now) or utcnow()
    tz = timezone(timedelta(minutes=tz_offset_min))
    cameras = _demo_sources(session)
    weights = [w for _n, _s, w, _z in CAMERAS]
    real_shifts = list(session.scalars(select(Shift).where(Shift.is_demo.is_(False))))

    local_now = now.astimezone(tz)
    first_day = (local_now - timedelta(days=days)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    slots = []
    start = first_day
    while start + timedelta(hours=SHIFT_HOURS) <= local_now:
        slots.append(start)
        start += timedelta(hours=SHIFT_HOURS)

    events = shifts = 0
    for i, local_start in enumerate(slots):
        name, mean_from, mean_to = SUPERVISORS[i % len(SUPERVISORS)]
        start = local_start.astimezone(UTC)
        end = start + timedelta(hours=SHIFT_HOURS)
        if any(
            as_utc(r.started_at) < end and (r.ended_at is None or as_utc(r.ended_at) > start)
            for r in real_shifts
        ):
            continue
        session.add(Shift(supervisor=name, site=None, started_at=start, ended_at=end, is_demo=True))
        shifts += 1
        progress = i / max(1, len(slots) - 1)
        mean = mean_from + (mean_to - mean_from) * progress
        for _ in range(_count(rng, mean)):
            events += _add_event(session, rng, cameras, weights, start, now)
    session.commit()
    return {"days": days, "shifts": shifts, "events": events, "sources": len(cameras)}


def _add_event(session, rng, cameras, weights, shift_start, now) -> int:
    source, zones = rng.choices(cameras, weights=weights)[0]
    zonal = [z for z in zones]
    scenario = rng.choices(list(SCENARIO_WEIGHTS), weights=list(SCENARIO_WEIGHTS.values()))[0]
    zone = None
    if scenario == ScenarioType.BLOCKED_EXIT:
        blocked = [z for z in zonal if "выход" in z.name.lower()]
        if not blocked:
            scenario = ScenarioType.DANGER_ZONE
        else:
            zone = blocked[0]
    if scenario == ScenarioType.DANGER_ZONE:
        danger = [z for z in zonal if "выход" not in z.name.lower()]
        if danger:
            zone = rng.choice(danger)
        else:  # у камеры выхода нет опасной зоны — там бывает только загромождение прохода
            scenario, zone = ScenarioType.BLOCKED_EXIT, zonal[0]
    started = shift_start + timedelta(seconds=rng.uniform(60, SHIFT_HOURS * 3600 - 600))
    duration = {
        ScenarioType.DANGER_ZONE: rng.uniform(2, 40),
        ScenarioType.RUN_FAST_MOVEMENT: rng.uniform(1, 6),
        ScenarioType.BLOCKED_EXIT: rng.uniform(60, 900),
    }[scenario]
    age_days = (now - started).total_seconds() / 86400
    if age_days > 2:
        review = rng.choices(
            [ReviewStatus.CONFIRMED, ReviewStatus.FALSE_POSITIVE, ReviewStatus.UNREVIEWED],
            weights=[0.68, 0.17, 0.15],
        )[0]
    else:
        review = rng.choices(
            [ReviewStatus.CONFIRMED, ReviewStatus.FALSE_POSITIVE, ReviewStatus.UNREVIEWED],
            weights=[0.3, 0.1, 0.6],
        )[0]
    video_start = rng.uniform(0, 3600)
    ended = started + timedelta(seconds=duration)
    session.add(
        Event(
            scenario_type=scenario,
            source_id=source.id,
            zone_id=zone.id if zone else None,
            track_id=None if scenario == ScenarioType.BLOCKED_EXIT else rng.randint(1, 400),
            priority=Priority.HIGH,
            state=EpisodeState.ENDED,
            review_status=review,
            confidence=round(rng.uniform(0.55, 0.97), 3),
            started_at=started,
            last_seen_at=ended,
            ended_at=ended,
            video_ts_start=video_start,
            video_ts_end=video_start + duration,
            video_ts_detected=video_start + min(duration, 1.0),
            detected_at=started + timedelta(seconds=1),
            received_at=started + timedelta(seconds=1),
            notified_at=started + timedelta(seconds=1),
            alerts_sent=1,
            model_version=DEMO_MODEL,
            viewed_at=ended if age_days > 1 else None,
            description="Демо-история",
        )
    )
    return 1
