"""Привязка предзаписанных детекций к источнику: загрузка, PATCH, REPLAY_DIR, воркер.

Записи — тестовые (FakeRuntime, model_version "TEST-not-linq"), не результаты LinQ.
"""

import threading
from pathlib import Path

import pytest

from app.models import ProcessingStat
from app.pipeline.backend_client import BackendClient
from app.pipeline.worker import EXIT_OK, EXIT_PIPELINE_ERROR, run_worker
from tests.conftest import INTERNAL_TOKEN
from tests.helpers import DEFAULT_ZONE
from tests.test_replay import FakeRuntime, generate, make_video

STAND = (140, 100, 190, 210)  # человек стоит в DEFAULT_ZONE всё видео (кадр 320×240)


@pytest.fixture
def pair(tmp_path: Path):
    video = make_video(tmp_path / "cam.mp4", seconds=3)
    jsonl = tmp_path / "cam.jsonl"
    generate(video, jsonl, FakeRuntime(STAND, (320, 240)))
    return video, jsonl


def _client(http) -> BackendClient:
    return BackendClient("http://testserver", INTERNAL_TOKEN, http=http, retries=0)


def _upload(client, video: Path, jsonl: Path | None = None):
    files = {"file": ("cam.mp4", video.read_bytes(), "video/mp4")}
    if jsonl is not None:
        files["replay"] = ("cam.jsonl", jsonl.read_bytes(), "application/json")
    return client.post("/api/v1/sources/upload", files=files, data={"name": "Камера 1"})


def _config(client, internal_headers, source_id):
    return client.get(f"/internal/sources/{source_id}/config", headers=internal_headers).json()


def test_upload_with_replay_binds_source_and_caches_sha(client, internal_headers, pair):
    video, jsonl = pair
    r = _upload(client, video, jsonl)
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["replay_path"].endswith(".jsonl") and len(src["video_sha256"]) == 64
    config = _config(client, internal_headers, src["id"])
    assert config["replay"]["origin"] == "source"
    assert config["replay"]["video_sha256"] == src["video_sha256"]


def test_upload_with_broken_replay_is_rejected_and_source_not_created(client, tmp_path, pair):
    video, _ = pair
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type": "header", "schema_version": 9}\n', encoding="utf-8")
    r = _upload(client, video, bad)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REPLAY"
    assert "версия схемы" in r.json()["error"]["message"]
    assert client.get("/api/v1/sources").json() == []


def test_patch_and_upload_replay_for_existing_source(client, tmp_path, pair):
    video, jsonl = pair
    src = _upload(client, video).json()
    r = client.patch(f"/api/v1/sources/{src['id']}", json={"replay_path": str(tmp_path / "no")})
    assert r.status_code == 422 and "не найден" in r.json()["error"]["message"]
    r = client.patch(f"/api/v1/sources/{src['id']}", json={"replay_path": str(jsonl)})
    assert r.status_code == 200 and r.json()["replay_path"] == str(jsonl)
    r = client.patch(f"/api/v1/sources/{src['id']}", json={"replay_path": ""})
    assert r.json()["replay_path"] is None
    r = client.post(
        f"/api/v1/sources/{src['id']}/replay",
        files={"file": ("x.jsonl", jsonl.read_bytes(), "application/json")},
    )
    assert r.status_code == 200 and r.json()["replay_path"] == f"replays/{src['id']}.jsonl"


def test_replay_dir_found_by_video_sha(client, settings, internal_headers, tmp_path, pair):
    video, jsonl = pair
    folder = tmp_path / "replays"
    folder.mkdir()
    (folder / "any-name.jsonl").write_bytes(jsonl.read_bytes())
    settings.replay_dir = str(folder)
    src = _upload(client, video).json()
    config = _config(client, internal_headers, src["id"])
    assert config["replay"]["origin"] == "replay_dir"
    other = _upload(client, make_video(tmp_path / "other.mp4", seconds=2)).json()
    config = _config(client, internal_headers, other["id"])
    assert config["replay"] is None and "нет записи с SHA-256" in config["replay_note"]


def test_worker_replay_run_events_and_separate_metrics(client, app, settings, pair):
    video, jsonl = pair
    src = _upload(client, video, jsonl).json()
    client.post(
        f"/api/v1/sources/{src['id']}/zones", json={"name": "Пресс", "polygon": DEFAULT_ZONE}
    )
    assert run_worker(src["id"], _client(client), settings, "mock", threading.Event()) == EXIT_OK
    events = client.get("/api/v1/events", params={"scenario_type": "danger_zone"}).json()
    assert events["total"] == 1 and events["items"][0]["model_version"] == "TEST-not-linq"
    first_id = events["items"][0]["id"]
    with app.state.session_factory() as s:
        stat = s.query(ProcessingStat).order_by(ProcessingStat.ts.desc()).first()
        assert stat.detector_kind == "test_not_linq_replay"
        assert stat.inference_ms_avg is None  # записанное время не выдаётся за замер
        assert stat.recorded_inference_ms_p95 is not None
        assert stat.replay_file == f"{src['id']}.jsonl"
    # Повторный прогон: свежие детектор, трекер, эпизоды; журнал без дублей.
    assert run_worker(src["id"], _client(client), settings, "mock", threading.Event()) == EXIT_OK
    again = client.get("/api/v1/events", params={"scenario_type": "danger_zone"}).json()
    assert again["total"] == 1 and again["items"][0]["id"] == first_id


def test_worker_replay_mismatch_and_missing_binding_are_errors(client, settings, tmp_path, pair):
    video, jsonl = pair
    other = make_video(tmp_path / "other.mp4", seconds=2)
    src = _upload(client, other).json()
    client.patch(f"/api/v1/sources/{src['id']}", json={"replay_path": str(jsonl)})
    code = run_worker(src["id"], _client(client), settings, "mock", threading.Event())
    assert code == EXIT_PIPELINE_ERROR
    error = client.get(f"/api/v1/sources/{src['id']}").json()["last_error"]
    assert "не соответствует видео" in error and "SHA-256" in error
    plain = _upload(client, video).json()
    code = run_worker(plain["id"], _client(client), settings, "replay", threading.Event())
    assert code == EXIT_PIPELINE_ERROR
    error = client.get(f"/api/v1/sources/{plain['id']}").json()["last_error"]
    assert "не найдена запись детекций" in error
