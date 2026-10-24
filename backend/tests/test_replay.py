"""ReplayDetector (schema v2) и генерация записи linq/run_linq_detections.py.

Синтетические записи — тестовые: runtime FakeRuntime, model_version "TEST-not-linq",
detector_kind "test_not_linq". Это не результаты LinQ.
"""

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.pipeline.detectors.replay import ReplayDetector, ReplayFileError, ReplayMissingFrame
from app.pipeline.detectors.replay_format import (
    ReplayFormatError,
    frame_line,
    header_line,
    index_path,
    load_index,
    sha256_file,
)
from app.pipeline.detectors.yolo_core import letterbox
from app.pipeline.episodes import REPLAY_EVENT_NAMESPACE
from app.pipeline.interfaces import Frame
from app.pipeline.runner import Pipeline
from app.pipeline.sources import VideoFileSource, analysis_frame_indices, probe_video
from tests.helpers import RecordingSink, make_config

ROOT = Path(__file__).resolve().parents[2]
TEST_MODEL = {"name": "TEST", "version": "TEST-not-linq", "sha256": None, "path": "test"}


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "run_linq_detections", ROOT / "linq" / "run_linq_detections.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def make_video(path: Path, seconds: float = 3.0, fps: int = 10, size=(320, 240)) -> Path:
    w, h = size
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for i in range(int(seconds * fps)):
        img = np.full((h, w, 3), 70, np.uint8)
        cv2.putText(img, str(i), (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        writer.write(img)
    writer.release()
    return path


class FakeRuntime:
    """ТЕСТ, не LinQ: выход сети YOLOv8 с одной рамкой «person» в заданных пикселях кадра."""

    def __init__(self, box_px, frame_size, score: float = 0.9, empty: bool = False) -> None:
        self.box, self.size, self.score, self.empty = box_px, frame_size, score, empty
        self.device_ms = None
        self.calls = 0

    def run(self, tensor):
        self.calls += 1
        out = np.zeros((1, 84, 8400), np.float32)
        if self.empty:
            return out
        w, h = self.size
        _, info = letterbox(np.zeros((h, w, 3), np.uint8), 640)
        x1, y1, x2, y2 = self.box
        s = info.scale
        out[0, 0, 0] = (x1 + x2) / 2 * s + info.pad_x
        out[0, 1, 0] = (y1 + y2) / 2 * s + info.pad_y
        out[0, 2, 0] = (x2 - x1) * s
        out[0, 3, 0] = (y2 - y1) * s
        out[0, 4, 0] = self.score  # класс 0 — person
        return out

    def describe(self):
        return {"name": "TEST — не LinQ"}


def generate(video: Path, out: Path, runtime, **kw) -> dict:
    kw.setdefault("warmup", 1)
    return gen.run(
        video, out, runtime, model=TEST_MODEL, detector_kind="test_not_linq", log=lambda *_: None,
        **kw,
    )  # fmt: skip


def frames(path: Path, analysis_fps: float, realtime: bool = False):
    return VideoFileSource("s", str(path), analysis_fps, realtime=realtime)


@pytest.fixture
def clip(tmp_path: Path):
    """Видео 320×240, 10 кадр/с, 3 с и полная запись к нему (человек в кадре всё время)."""
    video = make_video(tmp_path / "clip.mp4")
    out = tmp_path / "clip.jsonl"
    generate(video, out, FakeRuntime((140, 120, 190, 230), (320, 240)))
    return video, out


# --- генерация и формат -----------------------------------------------------------------------


def test_generation_writes_header_frames_summary(clip):
    video, out = clip
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    header, summary = lines[0], lines[-1]
    assert header["type"] == "header" and header["schema_version"] == 2
    assert header["video"]["sha256"] == sha256_file(video)
    assert header["video"]["width"] == 320 and header["video"]["fps"] == pytest.approx(10)
    assert header["model"]["version"] == "TEST-not-linq"
    assert summary["type"] == "summary" and summary["frames_written"] == 30
    assert summary["warmup_runs_excluded"] == 1 and summary["inference_ms"]["p95"] is not None
    frame = lines[1]
    assert frame["frame_idx"] == 0 and frame["video_ts"] == 0
    assert frame["detections"][0]["bbox"] == pytest.approx([140, 120, 190, 230], abs=0.6)
    assert "track_id" not in frame["detections"][0]


@pytest.mark.parametrize("size", [(320, 240), (240, 320), (256, 256)])
def test_letterbox_back_to_pixels_any_orientation(tmp_path, size):
    w, h = size
    box = (0.2 * w, 0.3 * h, 0.5 * w, 0.9 * h)
    video = make_video(tmp_path / "v.mp4", seconds=0.3, size=size)
    out = tmp_path / "v.jsonl"
    generate(video, out, FakeRuntime(box, size))
    detector = ReplayDetector(out)
    dets = detector.infer(Frame("s", 0, 0.0, w, h)).detections
    assert dets[0].bbox == pytest.approx((0.2, 0.3, 0.5, 0.9), abs=0.004)


def test_resume_after_interrupt_and_refuse_finished(tmp_path, clip):
    video, full = clip
    part = tmp_path / "part.jsonl"
    lines = full.read_text(encoding="utf-8").splitlines(keepends=True)
    part.write_text("".join(lines[:11]) + lines[11][:15], encoding="utf-8")  # оборвано на записи
    runtime = FakeRuntime((140, 120, 190, 230), (320, 240))
    summary = generate(video, part, runtime)
    assert summary["frames_written"] == 30
    assert runtime.calls == 1 + 20  # прогрев + кадры 10..29
    idx = [json.loads(x).get("frame_idx") for x in part.read_text(encoding="utf-8").splitlines()]
    assert idx[1:-1] == list(range(30))
    with pytest.raises(RuntimeError, match="уже завершён"):
        generate(video, part, runtime)
    broken = tmp_path / "broken.jsonl"
    broken.write_text(lines[0] + "{oops\n" + lines[2], encoding="utf-8")
    with pytest.raises(RuntimeError, match="повреждён"):
        generate(video, broken, runtime)


# --- сопоставление по frame_idx ---------------------------------------------------------------


@pytest.mark.parametrize("analysis_fps", [10, 5, 3])
def test_records_matched_by_source_frame_idx(clip, analysis_fps):
    video, out = clip
    detector = ReplayDetector(out, analysis_fps)
    info = probe_video(str(video))
    detector.verify_video(sha256=sha256_file(video), **info)
    planned = analysis_frame_indices(info["fps"], info["frame_count"], analysis_fps)
    detector.check_schedule(planned)
    seen = []
    for frame in frames(video, analysis_fps):
        dets = detector.infer(frame)
        assert dets.frame_idx == frame.frame_idx and dets.video_ts == frame.video_ts
        assert dets.analyzed and len(dets.detections) == 1
        seen.append(frame.frame_idx)
    assert seen == planned


def test_step_recording_incompatible_analysis_fps(tmp_path):
    video = make_video(tmp_path / "v.mp4", seconds=2, fps=30)
    out = tmp_path / "v.jsonl"
    generate(video, out, FakeRuntime((10, 10, 50, 100), (320, 240)), step=3)
    detector = ReplayDetector(out, 10)
    info = probe_video(str(video))
    detector.check_schedule(analysis_frame_indices(info["fps"], info["frame_count"], 10))  # 0,3,6…
    with pytest.raises(ReplayFileError, match="несовместим"):
        detector.check_schedule(analysis_frame_indices(info["fps"], info["frame_count"], 4))


def test_empty_detections_is_analyzed_frame(tmp_path):
    video = make_video(tmp_path / "v.mp4", seconds=0.5)
    out = tmp_path / "v.jsonl"
    generate(video, out, FakeRuntime(None, (320, 240), empty=True))
    dets = ReplayDetector(out).infer(Frame("s", 2, 0.2, 320, 240))
    assert dets.analyzed and dets.detections == []


def _drop_frames(src: Path, dst: Path, drop: set[int]) -> Path:
    keep = []
    for line in src.read_text(encoding="utf-8").splitlines(keepends=True):
        record = json.loads(line)
        if record.get("frame_idx") in drop:
            continue
        keep.append(line)
    dst.write_text("".join(keep), encoding="utf-8")
    return dst


def test_missing_record_error_and_unknown_policies(tmp_path, clip):
    _, out = clip
    holed = _drop_frames(out, tmp_path / "holed.jsonl", {12})
    with pytest.raises(ReplayMissingFrame, match="нет кадра 12"):
        ReplayDetector(holed).infer(Frame("s", 12, 1.2, 320, 240))
    dets = ReplayDetector(holed, on_missing="unknown").infer(Frame("s", 12, 1.2, 320, 240))
    assert not dets.analyzed and "нет кадра 12" in dets.unanalyzed_reason


def test_jsonl_shorter_than_video(tmp_path, clip):
    _, out = clip
    short = _drop_frames(out, tmp_path / "short.jsonl", set(range(20, 30)))
    with pytest.raises(ReplayMissingFrame, match="закончилась раньше видео"):
        ReplayDetector(short).infer(Frame("s", 25, 2.5, 320, 240))


def test_unknown_frames_do_not_end_episode_or_open_new(tmp_path, clip):
    """Пропуск записи в середине эпизода: «оценить нельзя», эпизод не рвётся (как при
    перекрытии), зоны — unknown с причиной."""
    video, out = clip
    holed = _drop_frames(out, tmp_path / "holed.jsonl", {15, 16, 17})
    config = make_config([[[0.3, 0.3], [0.8, 0.3], [0.8, 1.0], [0.3, 1.0]]])
    sink = RecordingSink()
    detector = ReplayDetector(holed, on_missing="unknown")
    pipeline = Pipeline(config, detector, sink)
    states = []
    pipeline.run(frames(video, 10), on_frame=lambda f, r: states.append((f.frame_idx, r)))
    (episode,) = sink.episodes()
    assert episode["video_ts_end"] == pytest.approx(2.9)
    result = dict(states)[16]
    assert result.zone_states[0]["state"] == "unknown"
    assert "нет кадра 16" in result.zone_states[0]["reason"]
    assert pipeline.stats.frames_unanalyzed == 3


# --- сверка с видео и повреждения -------------------------------------------------------------


def test_video_mismatch_lists_differences(tmp_path, clip):
    video, out = clip
    detector = ReplayDetector(out)
    with pytest.raises(ReplayFileError) as err:
        detector.verify_video(sha256="0" * 64, fps=25.0, width=640, height=480, frame_count=99)
    text = str(err.value)
    assert "SHA-256" in text and "FPS" in text and "разрешение" in text and "кадров" in text
    other = make_video(tmp_path / "other.mp4", seconds=2, size=(320, 240))
    with pytest.raises(ReplayFileError, match="SHA-256"):
        detector.verify_video(sha256=sha256_file(other), **probe_video(str(other)))


def _header(**over) -> str:
    meta = {
        "video": {"sha256": "a" * 64, "fps": 10, "width": 100, "height": 100},
        "step": 1,
        "model": TEST_MODEL,
        "detector_kind": "test_not_linq",
        **over,
    }
    return header_line(meta)


def _frame(idx: int, bbox=(10, 10, 50, 90), **det) -> str:
    return frame_line(
        idx, idx / 10, [{"label": "person", "confidence": 0.9, "bbox": list(bbox), **det}],
        inference_ms=5.0,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (_header() + _frame(0) + "{not json\n", r"bad\.jsonl:3: повреждённый JSON"),
        (_header() + _frame(0) + _frame(0), "повтор frame_idx 0"),
        (_header() + _frame(0, bbox=(10, 10, 150, 90)), "вне кадра"),
        (_header() + _frame(0, bbox=(50, 10, 10, 90)), "x2 < x1"),
        (_header() + _frame(0, track_id=3), "track_id в записи детектора запрещён"),
        (_header().replace('"schema_version": 2', '"schema_version": 7') + _frame(0), "версия"),
        ("", "пуст"),
    ],
)
def test_corrupted_files_are_rejected_with_reason(tmp_path, body, message):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(body, encoding="utf-8")
    with pytest.raises(ReplayFormatError, match=message):
        ReplayDetector(bad)


def test_missing_file(tmp_path):
    with pytest.raises(ReplayFileError, match="не найден"):
        ReplayDetector(tmp_path / "nope.jsonl")


def test_index_is_built_once_and_rebuilt_when_file_changes(tmp_path):
    path = tmp_path / "r.jsonl"
    path.write_text(_header() + _frame(0) + _frame(1), encoding="utf-8")
    first = load_index(path)
    idx_file = index_path(path)
    assert idx_file.is_file() and json.loads(idx_file.read_text())["jsonl_sha256"]
    assert load_index(path)["jsonl_sha256"] == first["jsonl_sha256"]  # из кэша
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_frame(2))
    second = load_index(path)
    assert second["jsonl_sha256"] != first["jsonl_sha256"] and len(second["offsets"]) == 3
    detector = ReplayDetector(path)
    assert detector.infer(Frame("s", 2, 0.2, 100, 100)).detections[0].bbox[0] == pytest.approx(0.1)


def test_legacy_v1_is_read_with_explicit_mark(tmp_path):
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps({"frame_idx": 0, "video_ts": 0, "frame_w": 10, "frame_h": 10,
                    "inference_ms": 1, "model_version": "m", "detections": [
                        {"label": "person", "confidence": 0.8, "bbox": [0.1, 0.1, 0.5, 0.9]}]})
        + "\n",
        encoding="utf-8",
    )  # fmt: skip
    detector = ReplayDetector(path)
    assert detector.legacy and "v1, без метаданных" in detector.model_version
    assert detector.infer(Frame("s", 0, 0, 10, 10)).detections[0].bbox == (0.1, 0.1, 0.5, 0.9)


# --- конвейер ---------------------------------------------------------------------------------

STAND_ZONE = [[0.3, 0.3], [0.8, 0.3], [0.8, 1.0], [0.3, 1.0]]


def _run(config, path: Path, video: Path, realtime: bool = False) -> RecordingSink:
    sink = RecordingSink()
    detector = ReplayDetector(path)
    Pipeline(config, detector, sink, event_id_seed=detector.jsonl_sha256).run(
        frames(video, 10, realtime)
    )
    return sink


def test_end_to_end_video_replay_tracker_rule_event(clip):
    """Видео → ReplayDetector → трекер → правило «опасная зона» → событие."""
    video, out = clip
    config = make_config([STAND_ZONE])
    sink = _run(config, out, video)
    (episode,) = sink.episodes()
    assert episode["scenario_type"] == "danger_zone" and episode["track_id"] is not None
    assert episode["model_version"] == "TEST-not-linq"


def test_rerun_fresh_state_same_event_ids(clip):
    video, out = clip
    config = make_config([STAND_ZONE])
    first, second = _run(config, out, video), _run(config, out, video)
    assert first.event_ids() == second.event_ids() and len(first.event_ids()) == 1
    assert uuid.UUID(first.event_ids()[0]).version == 5
    # Новое состояние трекера и эпизодов: тот же эпизод по времени видео и тот же трек.
    keys = ("video_ts_start", "video_ts_detected", "video_ts_end", "track_id", "zone_id")
    assert [{k: e[k] for k in keys} for e in first.episodes()] == [
        {k: e[k] for k in keys} for e in second.episodes()
    ]
    other_seed = RecordingSink()
    Pipeline(config, ReplayDetector(out), other_seed, event_id_seed="другая запись").run(
        frames(video, 10)
    )
    assert other_seed.event_ids() != first.event_ids()
    assert uuid.uuid5(REPLAY_EVENT_NAMESPACE, "x").version == 5


def test_realtime_and_fast_give_same_timeline(tmp_path):
    video = make_video(tmp_path / "short.mp4", seconds=1.5)
    out = tmp_path / "short.jsonl"
    generate(video, out, FakeRuntime((140, 120, 190, 230), (320, 240)))
    config = make_config([STAND_ZONE])
    fast, live = _run(config, out, video), _run(config, out, video, realtime=True)
    keys = ("video_ts_start", "video_ts_detected", "video_ts_end", "state")
    assert [{k: e[k] for k in keys} for e in fast.episodes()] == [
        {k: e[k] for k in keys} for e in live.episodes()
    ]


def test_two_sources_with_own_records_are_independent(tmp_path):
    a = make_video(tmp_path / "a.mp4", seconds=1, size=(320, 240))
    b = make_video(tmp_path / "b.mp4", seconds=1, size=(240, 320))
    ja, jb = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    generate(a, ja, FakeRuntime((10, 10, 60, 200), (320, 240)))
    generate(b, jb, FakeRuntime(None, (240, 320), empty=True))
    da, db = ReplayDetector(ja), ReplayDetector(jb)
    for i in range(10):
        assert len(da.infer(Frame("a", i, i / 10, 320, 240)).detections) == 1
        assert db.infer(Frame("b", i, i / 10, 240, 320)).detections == []
    assert da.jsonl_sha256 != db.jsonl_sha256
