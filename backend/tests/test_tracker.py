"""ByteTrack: стабильность ID, перекрытия, слабые детекции, разная частота кадров."""

import time
from dataclasses import replace

from app.pipeline.interfaces import Detection, FrameDetections
from app.pipeline.tracker import ByteTrackTracker, IoUTracker, TrackerConfig, build_tracker

W, H = 1280, 720


def person(
    foot_x: float, foot_y: float = 0.9, h: float = 0.3, conf: float = 0.85, tid=None
) -> Detection:
    w = h * 0.4 * H / W
    return Detection(label="person", confidence=conf, track_id=tid,
                     bbox=(foot_x - w / 2, foot_y - h, foot_x + w / 2, foot_y))  # fmt: skip


def frame(ts: float, *dets: Detection) -> FrameDetections:
    return FrameDetections(frame_idx=round(ts * 100), video_ts=ts, frame_w=W, frame_h=H,
                           inference_ms=1.0, model_version="t", detections=list(dets))  # fmt: skip


def run(tracker, fps: float, duration: float, scene) -> dict[float, list]:
    """scene(ts) -> список Detection; возвращает снимки подтверждённых треков по кадрам.

    Снимки (копии), потому что ByteTrack обновляет один и тот же объект Track от кадра к кадру."""
    out = {}
    for i in range(int(duration * fps) + 1):
        ts = round(i / fps, 4)
        out[ts] = [
            replace(t) for t in tracker.update(frame(ts, *scene(ts))) if t.state == "confirmed"
        ]
    return out


def ids(result) -> set[int]:
    return {t.track_id for tracks in result.values() for t in tracks}


def test_stable_id_on_smooth_trajectory():
    # Ходьба 1 рост/с: при 10 кадрах/с — 0.1 роста за кадр.
    res = run(ByteTrackTracker(), 10, 4, lambda ts: [person(0.1 + ts * 0.3 * H / W)])
    assert len(ids(res)) == 1


def test_new_track_needs_min_hits_and_single_frame_noise_is_ignored():
    tracker = ByteTrackTracker(TrackerConfig(min_hits=3))
    states = [tracker.update(frame(i / 10, person(0.5)))[0].state for i in range(3)]
    assert states == ["tentative", "tentative", "confirmed"]
    noise = run(ByteTrackTracker(), 10, 2, lambda ts: [person(0.3)] if ts == 1.0 else [])
    assert ids(noise) == set()


def test_short_loss_keeps_id_long_loss_gives_new_one():
    scene = lambda ts: [] if 2.0 <= ts < 2.6 else [person(0.2 + 0.05 * ts)]  # noqa: E731
    assert len(ids(run(ByteTrackTracker(TrackerConfig(buffer_s=2.0)), 10, 5, scene))) == 1
    scene = lambda ts: [] if 2.0 <= ts < 4.5 else [person(0.2 + 0.05 * ts)]  # noqa: E731
    assert len(ids(run(ByteTrackTracker(TrackerConfig(buffer_s=2.0)), 10, 6, scene))) == 2


def test_lost_track_is_reported_with_predicted_box_then_removed():
    tracker = ByteTrackTracker(TrackerConfig(buffer_s=1.0))
    for i in range(10):
        tracker.update(frame(i / 10, person(0.2 + 0.02 * i)))
    lost = tracker.update(frame(1.0))
    assert [t.state for t in lost] == ["lost"]
    assert lost[0].bbox[0] > 0.2 + 0.02 * 9 - 0.05  # прогноз продолжает движение
    assert tracker.update(frame(2.5)) == []  # дольше буфера — удалён


def test_low_confidence_detections_extend_track():
    # Человек частично закрыт: уверенность падает до 0.2 (ниже high=0.5), трек не рвётся.
    scene = lambda ts: [person(0.3 + 0.03 * ts, conf=0.2 if 2 <= ts < 4 else 0.85)]  # noqa: E731
    res = run(ByteTrackTracker(), 10, 6, scene)
    assert len(ids(res)) == 1
    assert all(len(tracks) == 1 for ts, tracks in res.items() if 2 <= ts < 4)
    # IoU-трекер слабые детекции не видит: на эти 2 с человек пропадает.
    iou = run(build_tracker(TrackerConfig(kind="iou", buffer_s=1.0)), 10, 6, scene)
    assert len(ids(iou)) == 2


def test_two_people_crossing_current_behaviour():
    """Двое идут навстречу и расходятся. Фиксируем фактическое поведение: ровно два ID,
    и каждый остаётся у своего человека (сопоставление по прогнозу Калмана)."""
    a = lambda ts: 0.2 + 0.06 * ts  # noqa: E731
    b = lambda ts: 0.8 - 0.06 * ts  # noqa: E731
    res = run(
        ByteTrackTracker(), 10, 10, lambda ts: [person(a(ts), conf=0.9), person(b(ts), conf=0.8)]
    )
    assert len(ids(res)) == 2
    first = {t.track_id: t.bbox[0] for t in res[1.0]}
    last = {t.track_id: t.bbox[0] for t in res[10.0]}
    left_id = min(first, key=first.get)
    assert last[left_id] > 0.5  # тот, кто шёл слева направо, справа


def test_fast_runner_keeps_id_at_5_and_15_fps():
    # Бег 2.6 роста/с узкой фигуры: при 5 кадрах/с рамки соседних кадров не пересекаются.
    speed = 2.6 * 0.2 * H / W  # доли ширины в секунду
    for fps in (5, 15):
        res = run(ByteTrackTracker(), fps, 2.5, lambda ts: [person(0.05 + speed * ts, h=0.2)])
        assert len(ids(res)) == 1, fps


def test_history_in_pixels_and_trimmed():
    tracker = ByteTrackTracker()
    for i in range(60):
        tracks = tracker.update(frame(i / 10, person(0.2)))
    hist = tracks[0].history
    assert hist[-1].video_ts - hist[0].video_ts <= 3.0
    assert abs(hist[-1].foot_x - 0.2 * W) < 1 and abs(hist[-1].bbox_h - 0.3 * H) < 1


def test_passthrough_and_iou_tracker_still_work():
    tracks = ByteTrackTracker().update(frame(0.0, person(0.4, tid=7)))
    assert [t.track_id for t in tracks] == [7]
    iou = IoUTracker()
    assert (
        iou.update(frame(0.0, person(0.4)))[0].track_id
        == iou.update(frame(0.1, person(0.41)))[0].track_id
    )


def test_tracker_and_rules_speed_with_ten_people():
    from app.pipeline.runner import Pipeline
    from app.pipeline.sources import NullFrameSource
    from tests.helpers import RecordingSink, make_config

    class Crowd:
        kind, model_version = "mock", "crowd"

        def infer(self, f):
            dets = [
                person(0.05 + 0.09 * k + 0.01 * (f.video_ts % 3), 0.5 + 0.04 * k, 0.15)
                for k in range(10)
            ]
            return frame(f.video_ts, *dets)

        def close(self):
            pass

    cfg = make_config(scenario_type="run_fast_movement", zone_rule=False)
    pipeline = Pipeline(cfg, Crowd(), RecordingSink())
    frames = list(NullFrameSource(cfg.source.id, 20, 10, W, H))
    t0 = time.perf_counter()
    for f in frames:
        pipeline.process_frame(f)
    per_frame_ms = (time.perf_counter() - t0) * 1000 / len(frames)
    print(f"трекер + правила, 10 человек: {per_frame_ms:.2f} мс/кадр")
    assert per_frame_ms < 25
