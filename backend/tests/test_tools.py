"""Утилиты: валидатор детекций, seed, демо-видео."""

import json
from pathlib import Path

import cv2
import pytest

from app.db import make_engine, make_session_factory
from app.models import Rule, Source, Zone
from tests.fixtures.make_detections_sample import SAMPLE_PATH
from tools.make_demo_video import make_demo_video
from tools.seed import seed
from tools.validate_detections import main as validate_main
from tools.validate_detections import validate_file

GOOD = {"frame_idx": 0, "video_ts": 0.0, "frame_w": 640, "frame_h": 480, "inference_ms": 5.0,
        "model_version": "m", "detections": [
            {"label": "person", "confidence": 0.9, "bbox": [0.1, 0.1, 0.3, 0.9]}]}  # fmt: skip


def _write(path: Path, records: list) -> Path:
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def test_validator_accepts_sample():
    report = validate_file(SAMPLE_PATH)
    assert report.ok and report.frames == 261 and report.labels["person"] > 0


def test_validator_reports_lines(tmp_path, capsys, monkeypatch):
    bad_bbox = GOOD | {"frame_idx": 2, "video_ts": 0.2,
                       "detections": [{"label": "person", "confidence": 0.9,
                                       "bbox": [0.5, 0.1, 0.3, 1.4]}]}  # fmt: skip
    path = _write(tmp_path / "d.jsonl", [
        GOOD,
        GOOD | {"frame_idx": 1, "video_ts": 0.1},
        bad_bbox,                                            # строка 3: bbox
        "{oops",                                             # строка 4: не JSON
        GOOD | {"frame_idx": 4, "video_ts": 0.05},           # строка 5: video_ts убывает
        GOOD | {"frame_idx": 5, "video_ts": 0.5, "confidance": 1},  # строка 6: опечатка
    ])  # fmt: skip
    report = validate_file(path)
    assert [line for line, _ in report.errors] == [3, 4, 5, 6]
    assert "detections.0.bbox" in report.errors[0][1]
    assert "убывает" in report.errors[2][1]
    assert "confidance" in report.errors[3][1]

    monkeypatch.setattr("sys.argv", ["validate_detections.py", str(path)])
    assert validate_main() == 1
    out = capsys.readouterr().out
    assert "строка 3" in out and "ИТОГ: есть ошибки (4)" in out


def test_validator_warnings_do_not_fail(tmp_path):
    mixed = GOOD | {"frame_idx": 1, "video_ts": 0.1, "detections": [
        {"label": "person", "confidence": 0.9, "bbox": [0.1, 0.1, 0.3, 0.9], "track_id": 3},
        {"label": "person", "confidence": 0.8, "bbox": [0.5, 0.1, 0.7, 0.9]}]}  # fmt: skip
    report = validate_file(_write(tmp_path / "d.jsonl", [GOOD, mixed]))
    assert report.ok and any("track_id" in msg for _, msg in report.warnings)


def test_validator_empty_file(tmp_path):
    assert not validate_file(_write(tmp_path / "d.jsonl", [])).ok


@pytest.fixture(scope="module")
def demo_video(tmp_path_factory) -> Path:
    return make_demo_video(tmp_path_factory.mktemp("demo") / "demo.mp4", (320, 180), 10)


def test_demo_video(demo_video):
    cap = cv2.VideoCapture(str(demo_video))
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) == pytest.approx(260, abs=2)
    cap.release()


def test_seed_is_idempotent(settings, demo_video):
    first = seed(settings, demo_video)
    again = seed(settings, demo_video)
    assert first.created and not again.created
    assert again.source_id == first.source_id
    engine = make_engine(settings.database_url)
    with make_session_factory(engine)() as s:
        assert s.query(Source).count() == s.query(Zone).count() == s.query(Rule).count() == 1
        rule = s.get(Rule, first.rule_id)
        assert rule.zone_id == first.zone_id and rule.params["confirm_window_s"] == 1.0
    engine.dispose()


def test_seed_rejects_non_video(settings, tmp_path):
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"nope")
    with pytest.raises(SystemExit, match="не читается"):
        seed(settings, fake)


def test_seeded_source_works_with_api(client, settings, demo_video):
    """Фронт сразу может работать: источник, зона, правило и превью доступны через API."""
    result = seed(settings, demo_video)
    sources = client.get("/api/v1/sources").json()
    assert [s["id"] for s in sources] == [result.source_id]
    assert len(client.get(f"/api/v1/sources/{result.source_id}/zones").json()) == 1
    assert client.get("/api/v1/rules").json()[0]["scenario_type"] == "danger_zone"
    assert client.get(f"/api/v1/sources/{result.source_id}/preview").status_code == 200
