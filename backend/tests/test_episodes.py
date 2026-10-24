"""Общая машина состояний EpisodeManager: VIOLATION / HOLD / UNKNOWN / нет хита, зонные эпизоды,
zone_states; правила — без состояния."""

import uuid
from datetime import UTC, datetime

import pytest

from app.pipeline.episodes import EpisodeManager, RuleContext
from app.pipeline.interfaces import BaseScenarioParams, Frame, HitLevel, RuleHit, Track
from app.pipeline.scenarios.danger_zone import DangerZoneParams, DangerZoneRule
from app.pipeline.scenarios.run_fast_movement import RunFastMovementRule, RunFastParams
from app.schemas.enums import Priority, ScenarioType
from tests.helpers import RecordingSink, make_context

T0 = datetime(2026, 10, 24, tzinfo=UTC)
SRC, ZONE, RULE = (str(uuid.uuid4()) for _ in range(3))
V, H, U = HitLevel.VIOLATION, HitLevel.HOLD, HitLevel.UNKNOWN


def manager(params: BaseScenarioParams, zone_ids=(ZONE,)) -> tuple[EpisodeManager, RecordingSink]:
    sink = RecordingSink()
    rule = RuleContext(RULE, ScenarioType.DANGER_ZONE, "Тест", Priority.HIGH, 1, params, zone_ids)
    return EpisodeManager(SRC, "Камера", {RULE: rule}, {}, sink, "m", T0), sink


def hit(level, ts, track_id: int | None = 1, zone_id=ZONE, **extra) -> RuleHit:
    return RuleHit(ScenarioType.DANGER_ZONE, RULE, track_id, zone_id, 0.9,
                   (0.1, 0.1, 0.2, 0.5) if track_id is not None else None, ts,
                   level=level, extra=extra or None)  # fmt: skip


def person(track_id=1) -> Track:
    return Track(track_id, "person", (0.1, 0.1, 0.2, 0.5), 0.9, 0.0, 0.0)


def run(em: EpisodeManager, script, fps=10, duration=10, present=True):
    """script(ts) -> уровень или None (нет хита). present — виден ли трек на кадре."""
    for i in range(int(duration * fps) + 1):
        ts = round(i / fps, 3)
        level = script(ts)
        hits = [hit(level, ts)] if level else []
        visible = present(ts) if callable(present) else present
        em.process(Frame("src", i, ts, 1280, 720), hits, [person()] if visible else [])
    em.finish()


def test_hysteresis_violation_then_hold_then_nothing():
    # VIOLATION 1–2 с, HOLD 2–4 с (продолжает), затем явно «нет» → конец через end_window_s.
    em, sink = manager(
        BaseScenarioParams(confirm_window_s=0.5, end_window_s=1.0, gap_tolerance_s=3)
    )
    run(em, lambda t: V if 1 <= t < 2 else H if 2 <= t < 4 else None)
    (ep,) = sink.episodes()
    assert ep["video_ts_start"] == 1.0 and ep["video_ts_detected"] == 1.5
    assert ep["video_ts_end"] == pytest.approx(3.9)  # последний HOLD
    ended = [p for _, p in sink.events if p["state"] == "ended"][0]
    assert ended["ended_at"]  # завершён по end_window_s, а не концом видео
    assert len(sink.event_ids()) == 1


def test_hold_alone_does_not_open_candidate():
    em, sink = manager(BaseScenarioParams(confirm_window_s=0.5))
    run(em, lambda t: H if 1 <= t < 5 else None)
    assert sink.episodes() == []


def test_unknown_neither_continues_nor_breaks_until_gap():
    params = BaseScenarioParams(confirm_window_s=0.5, end_window_s=0.5, gap_tolerance_s=2.0)
    # Короткий UNKNOWN (1 с) посреди нарушения — эпизод один.
    em, sink = manager(params)
    run(em, lambda t: V if 1 <= t < 3 or 4 <= t < 6 else U if 3 <= t < 4 else None)
    assert len(sink.episodes()) == 1
    # Длинный UNKNOWN (3 с > gap) — эпизод закончился, после него — новый.
    em, sink = manager(params)
    run(em, lambda t: V if 1 <= t < 3 or 6 <= t < 8 else U if 3 <= t < 6 else None)
    first, second = sink.episodes()
    assert first["video_ts_end"] == pytest.approx(2.9) and second["video_ts_start"] == 6.0


def test_lost_track_counts_as_unknown_not_as_no_violation():
    # Человек пропал на 1.5 с (меньше gap 2 с, больше end_window 0.5 с) — эпизод не рвётся.
    params = BaseScenarioParams(confirm_window_s=0.5, end_window_s=0.5, gap_tolerance_s=2.0)
    em, sink = manager(params)
    gone = lambda t: 3 <= t < 4.5  # noqa: E731
    run(
        em,
        lambda t: None if gone(t) else (V if 1 <= t < 7 else None),
        present=lambda t: not gone(t),
    )
    assert len(sink.episodes()) == 1


def test_danger_defaults_keep_old_behaviour():
    p = DangerZoneParams()
    assert p.end_window_s == p.gap_tolerance_s == 2.0


def test_zone_episode_without_track_and_zone_states():
    em, sink = manager(BaseScenarioParams(confirm_window_s=1.0, end_window_s=1.0))
    frame = lambda i: Frame("src", i, i / 10, 1280, 720)  # noqa: E731
    shapes = [{"polygon": [[0.2, 0.2], [0.4, 0.2], [0.4, 0.4]], "style": "violation", "label": "X"}]

    em.process(frame(0), [hit(U, 0.0, None, reason="сцена не готова")], [])
    assert em.zone_states() == [
        {
            "zone_id": ZONE,
            "scenario_type": "danger_zone",
            "state": "unknown",
            "reason": "сцена не готова",
        }
    ]
    em.process(frame(1), [hit(V, 0.1, None, shapes=shapes)], [])
    assert em.zone_states()[0]["state"] == "candidate"
    for i in range(2, 13):
        em.process(
            frame(i), [hit(V, i / 10, None, shapes=shapes, details={"blocked_pct": 0.1})], []
        )
    assert em.zone_states()[0]["state"] == "violation"
    em.finish()
    (ep,) = sink.episodes()
    assert ep["track_id"] is None and ep["details"]["blocked_pct_max"] == 0.1
    # Кадр-доказательство зонного нарушения: рамки фигур вместо рамки человека.
    meta = sink.evidence[-1][3]
    assert meta["bboxes"][0]["track_id"] is None and meta["bboxes"][0]["label"] == "X"


def test_rules_are_stateless():
    from collections import deque

    from app.pipeline.interfaces import TrackPoint, ZoneGeometry

    hist = deque(TrackPoint(t / 10, 100 + 40 * t, 600, 200) for t in range(10))
    t = Track(
        3, "person", (0.1, 0.3, 0.2, 0.83), 0.9, 0.0, 0.9, history=hist, frame_w=1280, frame_h=720
    )
    zone = ZoneGeometry("z", "Z", [(0.0, 0.5), (1.0, 0.5), (1.0, 1.0), (0.0, 1.0)])
    for rule, params, zones in (
        (RunFastMovementRule("r"), RunFastParams(), []),
        (DangerZoneRule("d"), DangerZoneParams(), [zone]),
    ):
        ctx = make_context([t], 0.9)
        first = rule.update(ctx, zones, params)
        second = rule.update(ctx, zones, params)
        assert first == second and first, rule
    run_rule = RunFastMovementRule("r")
    ctx = make_context([t], 0.9)
    assert run_rule.describe(ctx, [], RunFastParams()) == run_rule.describe(
        ctx, [], RunFastParams()
    )
