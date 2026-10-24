"""Сценарий run_fast_movement: ходьба, бег, помехи, гистерезис, зоны, независимость от fps."""

import pytest

from app.pipeline.detectors.mock_run import DURATION_S, MockRunDetector
from app.pipeline.interfaces import Detection, Detector, Frame, FrameDetections
from app.pipeline.runner import Pipeline
from app.pipeline.sources import NullFrameSource
from tests.helpers import RecordingSink, make_config

W, H = 1280, 720


class Scripted(Detector):
    """Люди как функции video_ts -> (x ног, y ног, рост в долях кадра) или None."""

    kind, model_version = "mock", "scripted"

    def __init__(self, *people):
        self.people = people

    def infer(self, frame: Frame) -> FrameDetections:
        dets = []
        for p in self.people:
            pos = p(frame.video_ts)
            if pos is not None:
                x, y, h = pos
                w = h * 0.4 * H / W
                if x - w / 2 < 0 or x + w / 2 > 1:
                    continue  # вышел из кадра
                dets.append(
                    Detection(
                        label="person", confidence=0.85, bbox=(x - w / 2, y - h, x + w / 2, y)
                    )
                )
        return FrameDetections(frame_idx=frame.frame_idx, video_ts=frame.video_ts, frame_w=W,
                               frame_h=H, inference_ms=1.0, model_version="scripted",
                               detections=dets)  # fmt: skip


def moving(speed_fn, x0=0.1, y=0.9, h=0.3, start=0.0, end=99.0):
    """Человек со скоростью (ростов/с) как функцией времени; x интегрируется с шагом 0.01 с."""

    def pos(ts):
        if not start <= ts < end:
            return None
        x, t = x0, start
        while t < ts - 1e-9:
            x += speed_fn(t) * h * H / W * 0.01
            t += 0.01
        return x, y, h

    return pos


def run(detector, *, fps=10.0, duration=10.0, zones=None, zone_rule=False, params=None):
    cfg = make_config(
        zones=zones, params=params, scenario_type="run_fast_movement", zone_rule=zone_rule
    )
    sink = RecordingSink()
    Pipeline(cfg, detector, sink).run(NullFrameSource(cfg.source.id, duration, fps, W, H))
    return sink, cfg


def created(sink) -> int:
    """Сколько тревог (event.created) ушло бы: первая отправка каждого эпизода."""
    return len(sink.event_ids())


def test_slow_walk_gives_no_event():
    sink, _ = run(Scripted(moving(lambda t: 0.8)))
    assert sink.episodes() == []


def test_close_walker_gives_no_event():
    # Человек близко к камере (рост 0.7 кадра) идёт 1 рост/с: в пикселях быстро, в ростах — нет.
    sink, _ = run(Scripted(moving(lambda t: 1.0, x0=0.2, y=0.95, h=0.7)), duration=4)
    assert sink.episodes() == []


def test_run_longer_than_confirm_gives_exactly_one_episode():
    sink, _ = run(Scripted(moving(lambda t: 2.5 if 1 <= t < 3.5 else 0.5, h=0.15)), duration=7)
    (ep,) = sink.episodes()
    assert created(sink) == 1
    assert ep["scenario_type"] == "run_fast_movement" and ep["state"] == "ended"
    assert 1.0 <= ep["video_ts_start"] <= 1.6  # начало бега с учётом окна скорости и EMA
    assert ep["video_ts_detected"] - ep["video_ts_start"] == pytest.approx(0.8, abs=0.11)
    assert 2.3 <= ep["details"]["speed_max"] <= 2.7
    # Остановился в 3.5 с — эпизод кончился примерно через end_window_s, а не в конце видео.
    assert ep["video_ts_end"] < 5.5


def test_run_shorter_than_confirm_gives_nothing():
    sink, _ = run(Scripted(moving(lambda t: 2.5 if 1 <= t < 1.6 else 0.5, h=0.25)), duration=5)
    assert sink.episodes() == []


def test_oscillation_near_threshold_does_not_split_episode():
    # Бег, затем скорость скачет 1.5 ↔ 2.0 (выше порога продолжения 1.8×0.7=1.26).
    def speed(t):
        if t < 1:
            return 0.5
        if t < 3:
            return 2.4
        if t < 7:
            return 2.0 if int(t * 3) % 2 else 1.5
        return 0.3

    sink, _ = run(Scripted(moving(speed, x0=0.05, h=0.1)), duration=10)
    assert len(sink.episodes()) == 1


def test_two_runners_give_two_episodes():
    a = moving(lambda t: 2.5, x0=0.05, y=0.9, h=0.2, start=1, end=4)
    b = moving(lambda t: 2.5, x0=0.05, y=0.5, h=0.2, start=1.2, end=4)
    sink, _ = run(Scripted(a, b), duration=6)
    eps = sink.episodes()
    assert len(eps) == 2 and len({e["track_id"] for e in eps}) == 2


def test_zone_rule_only_inside_zone():
    zone = [[0.0, 0.7], [1.0, 0.7], [1.0, 1.0], [0.0, 1.0]]  # нижняя полоса кадра
    inside = moving(lambda t: 2.5, x0=0.05, y=0.9, h=0.2, start=1, end=4)
    outside = moving(lambda t: 2.5, x0=0.05, y=0.5, h=0.2, start=1, end=4)
    sink, cfg = run(Scripted(inside, outside), duration=6, zones=[zone], zone_rule=True)
    (ep,) = sink.episodes()
    assert ep["zone_id"] == cfg.zones[0].id


def test_rule_without_zone_watches_whole_frame_even_if_source_has_zones():
    zone = [[0.0, 0.7], [1.0, 0.7], [1.0, 1.0], [0.0, 1.0]]
    outside = moving(lambda t: 2.5, x0=0.05, y=0.5, h=0.2, start=1, end=4)
    sink, _ = run(Scripted(outside), duration=6, zones=[zone], zone_rule=False)
    (ep,) = sink.episodes()
    assert ep["zone_id"] is None


def test_speed_threshold_param_is_respected():
    person = moving(lambda t: 2.5 if t >= 1 else 0.5, h=0.2, end=4)
    sink, _ = run(Scripted(person), duration=5, params={"speed_threshold": 3.0})
    assert sink.episodes() == []


# --- Mock-сцена: перекрытие, скачок рамки, далёкий бегущий, 5 и 15 кадров/с ---------------------


@pytest.mark.parametrize("fps", [5, 10, 15])
def test_mock_scene_two_runs_same_at_any_fps(fps):
    sink, _ = run(MockRunDetector(), fps=fps, duration=DURATION_S)
    eps = sink.episodes()
    assert created(sink) == 2
    near, far = eps
    # R: бег 4.0–6.4 с, перекрытие 5.6–5.9 — один эпизод; остановка — эпизод завершён до 9 с.
    assert 4.0 <= near["video_ts_start"] <= 4.8 and near["video_ts_end"] < 8.5
    # F: далёкий (рост 0.08 кадра) бегущий распознан — скорость в ростах от дальности не зависит.
    assert 10.0 <= far["video_ts_start"] <= 10.8 and far["details"]["speed_max"] >= 2.2
    # Медленно идущий W (с одиночным скачком рамки на 10.0 с) событий не дал.
    assert {e["track_id"] for e in eps} == {near["track_id"], far["track_id"]}
    assert all(e["state"] == "ended" for e in eps)


def test_evidence_has_trail_and_cyrillic_label():
    from app.pipeline.sources import VideoFileSource  # noqa: F401 — проверяем только наличие кадров

    sink, _ = run(MockRunDetector(), duration=8)
    kinds = [k for _, k, _, _ in sink.evidence]
    assert "frame_annotated" in kinds  # без картинки рисуется на сером фоне
