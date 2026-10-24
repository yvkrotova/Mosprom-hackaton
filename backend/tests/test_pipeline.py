"""Конвейер: MockDetector, ReplayDetector, трекер, DangerZoneRule, EpisodeManager, отрисовка."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.config import Settings
from app.pipeline.annotate import annotate_frame
from app.pipeline.detectors import build_detector
from app.pipeline.detectors.linq import LinqDetector
from app.pipeline.detectors.mock import DURATION_S, MockDetector
from app.pipeline.detectors.onnx import ModelFileError
from app.pipeline.detectors.replay import ReplayDetector, ReplayFileError, ReplayMissingFrame
from app.pipeline.interfaces import (
    Detection,
    Detector,
    Frame,
    FrameDetections,
    Track,
    ZoneGeometry,
)
from app.pipeline.runner import Pipeline
from app.pipeline.scenarios.danger_zone import DangerZoneParams, DangerZoneRule
from app.pipeline.sources import FrameSourceError, NullFrameSource, VideoFileSource
from app.pipeline.tracker import IoUTracker
from tests.fixtures.make_detections_sample import SAMPLE_PATH, SAMPLE_SIZE, SAMPLE_ZONE
from tests.fixtures.make_sample_video import make_sample_video
from tests.helpers import RecordingSink, make_config, make_context, summary

FPS = 10.0


def run_mock(config=None, frames=None) -> RecordingSink:
    config = config or make_config()
    sink = RecordingSink()
    detector = MockDetector(config.zones[0].polygon)
    frames = frames or NullFrameSource(config.source.id, DURATION_S + 1, FPS, *SAMPLE_SIZE)
    Pipeline(config, detector, sink).run(frames)
    return sink


# --- Тест 9: EpisodeManager на MockDetector --------------------------------------------------


def test_mock_scenario_gives_exactly_two_episodes():
    sink = run_mock()
    episodes = sink.episodes()
    assert len(episodes) == 2, summary(episodes)
    first, second = episodes

    # Основной вход: ноги пересекают границу около 3 с, стоит до ~11 с, перекрытие 7–8 с
    # (короче gap_tolerance 2 с) не разрывает эпизод.
    assert first["state"] == "ended"
    assert 2.5 <= first["video_ts_start"] <= 3.5
    assert first["video_ts_detected"] == pytest.approx(first["video_ts_start"] + 1.0, abs=0.01)
    assert 11.0 <= first["video_ts_end"] <= 12.5
    # Одиночный ложный кадр на 15 с — не событие.
    assert not any(14 <= e["video_ts_start"] <= 17 for e in episodes)
    # Второй вход после завершения первого — новое событие.
    assert 20.0 <= second["video_ts_start"] <= 21.0 and second["state"] == "ended"
    assert second["track_id"] != first["track_id"]


def test_one_alert_per_episode_and_throttled_updates():
    sink = run_mock()
    for event_id in sink.event_ids():
        sends = sink.sends(event_id)
        states = [s["state"] for s in sends]
        assert states[0] == "active" and states[-1] == "ended"
        assert states.count("ended") == 1
        # Каждое обновление несёт новое наблюдение, а их не больше, чем секунд в эпизоде.
        active_ts = [s["video_ts_end"] for s in sends if s["state"] == "active"]
        assert active_ts == sorted(set(active_ts))
        duration = sends[-1]["video_ts_end"] - sends[0]["video_ts_detected"]
        assert len(active_ts) - 1 <= duration / 1.0 + 1
    first = sink.sends(sink.event_ids()[0])
    assert 5 <= len(first) <= 11  # 1 создание + ~8 обновлений + завершение


def test_update_interval_is_respected():
    # Интервал 3 с: за ~8 с эпизода не больше 3 обновлений (против ~8 при 1 с).
    sink = run_mock(make_config(update_interval_s=3.0))
    first = sink.sends(sink.event_ids()[0])
    assert len([s for s in first if s["state"] == "active"]) - 1 <= 3


def test_payload_fields():
    config = make_config()
    sink = run_mock(config)
    created = sink.events[0][1]
    assert created["source_id"] == config.source.id
    assert created["zone_id"] == config.zones[0].id
    assert created["rule_id"] == config.rules[0].id and created["rule_version"] == 3
    assert created["priority"] == "high" and created["model_version"] == "mock-1.0"
    assert 0.6 <= created["confidence"] <= 0.99
    assert created["started_at"].endswith("Z") and created["ended_at"] is None
    assert "Зона 1" in created["description"]
    ended = sink.sends(sink.event_ids()[0])[-1]
    assert ended["ended_at"] == ended["last_seen_at"]
    # Без картинки (NullFrameSource) — только аннотированный кадр на сером фоне.
    assert [k for _, k, _, _ in sink.evidence] == ["frame_annotated", "frame_annotated"]


def test_short_gap_tolerance_splits_episode():
    # Перекрытие 1 с длиннее допуска 0.5 с — основной вход распадается на два эпизода.
    sink = run_mock(make_config(params={"gap_tolerance_s": 0.5}))
    assert len(sink.episodes()) == 3


def test_long_confirm_window_suppresses_short_entry():
    # Второй вход длится ~3 с: с окном подтверждения 5 с он не становится событием.
    sink = run_mock(make_config(params={"confirm_window_s": 5.0}))
    assert len(sink.episodes()) == 1


def test_disabled_track_label_filter():
    sink = run_mock(make_config(params={"labels": ["forklift"]}))
    assert sink.episodes() == []


def test_stop_midway_ends_active_episode():
    config = make_config()
    sink = RecordingSink()
    pipeline = Pipeline(config, MockDetector(config.zones[0].polygon), sink)
    frames = NullFrameSource(config.source.id, DURATION_S, FPS)
    pipeline.run(frames, should_stop=lambda: pipeline.stats.frames_processed >= 60)  # 6 с
    (episode,) = sink.episodes()
    assert episode["state"] == "ended" and episode["video_ts_end"] <= 6.0


# --- Тест 10: два человека в одной зоне ------------------------------------------------------


class ScriptedDetector(Detector):
    kind = "scripted"
    model_version = "scripted-1"

    def __init__(self, script):
        self.script = script  # video_ts -> list[Detection]

    def infer(self, frame: Frame) -> FrameDetections:
        return FrameDetections(
            source_id=frame.source_id, frame_idx=frame.frame_idx, video_ts=frame.video_ts,
            frame_w=frame.width, frame_h=frame.height, inference_ms=1.0,
            model_version=self.model_version, detections=self.script(frame.video_ts),
        )  # fmt: skip


def person(x: float, conf: float = 0.9) -> Detection:
    return Detection(label="person", confidence=conf, bbox=(x, 0.5, x + 0.08, 0.9))


def test_two_people_in_zone_give_two_episodes():
    config = make_config()
    sink = RecordingSink()

    def script(ts):
        return [person(0.40), person(0.60)] if 1.0 <= ts < 4.0 else []

    Pipeline(config, ScriptedDetector(script), sink).run(NullFrameSource(config.source.id, 8, FPS))
    episodes = sink.episodes()
    assert len(episodes) == 2
    assert {e["track_id"] for e in episodes} == {1, 2}
    assert all(e["zone_id"] == config.zones[0].id for e in episodes)


def test_one_person_in_two_zones_gives_two_episodes():
    left = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
    overlap = [[0.3, 0.0], [1.0, 0.0], [1.0, 1.0], [0.3, 1.0]]
    config = make_config([left, overlap], zone_rule=False)  # правило на все зоны
    sink = RecordingSink()
    Pipeline(config, ScriptedDetector(lambda ts: [person(0.4)] if ts < 3 else []), sink).run(
        NullFrameSource(config.source.id, 6, FPS)
    )
    assert {e["zone_id"] for e in sink.episodes()} == {z.id for z in config.zones}


# --- Тест 11: replay даёт те же эпизоды, что mock ---------------------------------------------


def test_replay_matches_mock():
    config = make_config([SAMPLE_ZONE])
    mock_sink = run_mock(config)

    replay_sink = RecordingSink()
    detector = ReplayDetector(SAMPLE_PATH, analysis_fps=FPS)
    frames = NullFrameSource(config.source.id, DURATION_S + 1, FPS, *SAMPLE_SIZE)
    Pipeline(config, detector, replay_sink).run(frames)

    assert len(replay_sink.episodes()) == 2
    assert summary(replay_sink.episodes()) == summary(mock_sink.episodes())
    # Старый формат (v1) читается только с явной пометкой.
    assert detector.model_version == "mock-1.0 (v1, без метаданных)"


def test_replay_lookup_and_errors(tmp_path: Path):
    detector = ReplayDetector(SAMPLE_PATH, analysis_fps=FPS)
    frame = Frame("s", 50, 5.0, 1280, 720)  # запись выбирается по frame_idx, не по времени
    assert detector.infer(frame).detections and detector.infer(frame).video_ts == 5.0
    # Кадра дальше конца записи нет — это ошибка, а не «людей нет».
    with pytest.raises(ReplayMissingFrame, match="закончилась раньше видео"):
        detector.infer(Frame("s", 4000, 400.0, 1280, 720))

    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"frame_idx":0,"video_ts":0,"frame_w":10,"frame_h":10,"inference_ms":1,'
        '"model_version":"m","detections":[]}\n{"frame_idx": 1}\n',
        encoding="utf-8",
    )
    with pytest.raises(ReplayFileError, match=":2:"):
        ReplayDetector(bad)
    with pytest.raises(ReplayFileError, match="не найден"):
        ReplayDetector(tmp_path / "missing.jsonl")


def test_sample_file_is_up_to_date(tmp_path: Path):
    """detections_sample.jsonl соответствует текущему MockDetector."""
    from tests.fixtures.make_detections_sample import make_detections_sample

    fresh = make_detections_sample(tmp_path / "fresh.jsonl")
    assert fresh.read_text(encoding="utf-8") == SAMPLE_PATH.read_text(encoding="utf-8"), (
        "Перегенерируйте: python -m tests.fixtures.make_detections_sample"
    )


# --- Видео: реальные кадры и доказательства --------------------------------------------------


@pytest.fixture(scope="module")
def demo_video(tmp_path_factory) -> Path:
    # 25 кадр/с в файле, анализ на 10 кадр/с — проверяем прореживание.
    return make_sample_video(tmp_path_factory.mktemp("demo") / "demo.mp4", DURATION_S + 1, 25)


def test_video_file_source_decimation(demo_video):
    frames = list(VideoFileSource("s", str(demo_video), analysis_fps=FPS))
    assert len(frames) == pytest.approx((DURATION_S + 1) * FPS, abs=3)
    steps = np.diff([f.video_ts for f in frames])
    assert steps.min() >= 0.079 and steps.max() <= 0.121
    assert frames[0].image.shape == (240, 320, 3) and frames[0].width == 320


def test_pipeline_on_real_video_sends_raw_and_annotated(demo_video):
    config = make_config()
    sink = run_mock(config, VideoFileSource(config.source.id, str(demo_video), FPS))
    assert len(sink.episodes()) == 2
    first_id = sink.event_ids()[0]
    kinds = [(eid, k) for eid, k, _, _ in sink.evidence]
    assert kinds[:2] == [(first_id, "frame_raw"), (first_id, "frame_annotated")]
    for _, kind, jpeg, meta in sink.evidence:
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if kind == "frame_raw":
            assert img.shape == (240, 320, 3)
        else:  # маленький кадр размечается в увеличенном виде, пропорции те же
            h, w = img.shape[:2]
            assert h >= 240 and w / h == pytest.approx(320 / 240, abs=0.01)
        assert meta["bboxes"][0]["label"] == "person" and meta["frame_time"].endswith("Z")


def test_video_source_open_error():
    with pytest.raises(FrameSourceError, match="Не удалось открыть"):
        list(VideoFileSource("s", "missing.mp4", FPS))


# --- Отдельные компоненты ---------------------------------------------------------------------


def _dets(ts, *dets) -> FrameDetections:
    return FrameDetections(frame_idx=0, video_ts=ts, frame_w=10, frame_h=10, inference_ms=0,
                           model_version="m", detections=list(dets))  # fmt: skip


def test_tracker_keeps_id_through_short_gap_and_drops_old():
    tracker = IoUTracker(max_age_s=2.0)
    (a,) = tracker.update(_dets(0.0, person(0.40)))
    (b,) = tracker.update(_dets(1.5, person(0.41)))  # пропуск 1.5 с — тот же трек
    assert b.track_id == a.track_id and b.first_seen_ts == 0.0
    (c,) = tracker.update(_dets(4.0, person(0.41)))  # пропуск 2.5 с — новый трек
    assert c.track_id != a.track_id


def test_tracker_passthrough():
    tracker = IoUTracker()
    dets = _dets(0.0, person(0.4).model_copy(update={"track_id": 77}))
    assert [t.track_id for t in tracker.update(dets)] == [77]


def test_danger_zone_uses_feet_point():
    rule = DangerZoneRule("r")
    zone = ZoneGeometry("z", "Z", [(0.0, 0.6), (1.0, 0.6), (1.0, 1.0), (0.0, 1.0)])
    params = DangerZoneParams()

    def track(bbox, label="person", conf=0.9):
        return Track(1, label, bbox, conf, 0.0, 0.0)

    def check(t):
        return rule.update(make_context([t]), [zone], params)

    assert check(track((0.4, 0.3, 0.5, 0.8)))  # ноги в зоне
    assert not check(track((0.4, 0.1, 0.5, 0.55)))  # ноги выше
    assert not check(track((0.4, 0.3, 0.5, 0.8), "forklift"))
    assert not check(track((0.4, 0.3, 0.5, 0.8), conf=0.3))


def test_annotate_without_image_keeps_size():
    out = annotate_frame(None, 640, 360, [ZoneGeometry("z", "Пресс", SAMPLE_ZONE)],
                         [Track(1, "person", (0.4, 0.4, 0.5, 0.9), 0.9, 0, 0)], 1, "z",
                         ["Человек в опасной зоне | Камера 1"])  # fmt: skip
    assert out.shape == (360, 640, 3)


def test_build_detector(tmp_path: Path):
    settings = Settings(_env_file=None, replay_path=str(SAMPLE_PATH))
    assert build_detector("mock", settings).kind == "mock"
    assert build_detector("replay", settings).kind == "replay"
    assert build_detector("linq", settings).kind == "linq_tpu"
    with pytest.raises(ModelFileError, match="не найден"):
        build_detector("onnx", Settings(_env_file=None, model_path=str(tmp_path / "no.onnx")))
    with pytest.raises(ValueError, match="REPLAY_PATH"):
        build_detector("replay", Settings(_env_file=None, replay_path=""))
    with pytest.raises(ValueError, match="Неизвестный"):
        build_detector("yolo", settings)


def test_linq_missing_configuration_is_error():
    detector = LinqDetector(Settings(_env_file=None))
    frame = Frame("s", 0, 0.0, 64, 48, np.zeros((48, 64, 3), np.uint8))
    with pytest.raises(ValueError, match="LINQ_API_URL"):
        detector.infer(frame)
