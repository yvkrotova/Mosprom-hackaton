"""Воркер, WorkerManager и контроль heartbeat.

In-process тесты гоняют run_worker() через TestClient; сквозные — настоящий subprocess
воркера против backend, поднятого uvicorn в фоновом потоке.
"""

import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Event, ProcessingStat, Source
from app.models.base import utcnow
from app.pipeline.backend_client import BackendClient
from app.pipeline.detectors.mock import DURATION_S
from app.pipeline.worker import EXIT_CONFIG_ERROR, EXIT_OK, EXIT_PIPELINE_ERROR, run_worker
from app.services import source_service
from tests.conftest import INTERNAL_TOKEN
from tests.fixtures.make_sample_video import make_sample_video
from tests.helpers import DEFAULT_ZONE


@pytest.fixture(scope="module")
def mock_video(tmp_path_factory) -> Path:
    """Видео на всю длину сценария MockDetector (маленькое — для скорости)."""
    path = tmp_path_factory.mktemp("worker") / "shift.mp4"
    return make_sample_video(path, DURATION_S + 1, 10, size=(192, 108))


@pytest.fixture
def configured_source(client, mock_video):
    """Источник с видео, зоной и правилом danger_zone — через публичный API."""

    def _make(uri: str | None = None) -> str:
        src = client.post(
            "/api/v1/sources", json={"name": "Камера 3", "uri": uri or str(mock_video)}
        ).json()
        zone = client.post(
            f"/api/v1/sources/{src['id']}/zones", json={"name": "Пресс", "polygon": DEFAULT_ZONE}
        ).json()
        rule = client.post(
            "/api/v1/rules",
            json={"source_id": src["id"], "zone_id": zone["id"], "scenario_type": "danger_zone"},
        )
        assert rule.status_code == 201, rule.text
        return src["id"]

    return _make


def _client(http: TestClient) -> BackendClient:
    return BackendClient("http://testserver", INTERNAL_TOKEN, http=http, retries=0)


# --- run_worker в процессе теста ----------------------------------------------------------


def test_worker_processes_video_and_reports(client, app, settings, configured_source):
    src = configured_source()
    code = run_worker(src, _client(client), settings, "mock", threading.Event())
    assert code == EXIT_OK

    source = client.get(f"/api/v1/sources/{src}").json()
    assert source["status"] == "stopped"  # конец видео
    events = client.get("/api/v1/events", params={"source_id": src}).json()["items"]
    assert len(events) == 2
    assert all(e["state"] == "ended" for e in events)
    for e in events:
        kinds = sorted(ev["kind"] for ev in e["evidence"])
        assert kinds == ["frame_annotated", "frame_raw"]
        for ev in e["evidence"]:
            assert (settings.storage_dir / ev["url"].removeprefix("/media/")).is_file()
    with app.state.session_factory() as s:
        stat = s.query(ProcessingStat).filter_by(source_id=src).order_by(ProcessingStat.ts).all()
    assert stat[-1].frames_processed == pytest.approx(DURATION_S * 10, abs=12)
    assert stat[-1].detector_kind == "mock" and stat[-1].inference_ms_avg > 0
    # Одна тревога на эпизод.
    created = [m for m in app.state.hub.history if m["type"] == "event.created"]
    assert len(created) == 2


def test_detector_error_sets_source_error(client, settings, configured_source):
    """Тест 12: ошибка детектора -> heartbeat status=error -> источник error."""
    src = configured_source()
    code = run_worker(src, _client(client), settings, "linq", threading.Event())
    assert code == EXIT_PIPELINE_ERROR
    source = client.get(f"/api/v1/sources/{src}").json()
    assert source["status"] == "error"
    assert "NotImplementedError" in source["last_error"]
    # Ошибка не выдаётся за «нарушений нет»: статус error виден, событий нет.
    assert client.get("/api/v1/events").json()["total"] == 0


def test_source_error_sets_source_error(client, settings, configured_source):
    src = configured_source(uri="missing/video.mp4")
    assert run_worker(src, _client(client), settings, "mock", threading.Event()) == 1
    source = client.get(f"/api/v1/sources/{src}").json()
    assert source["status"] == "error" and "Не удалось открыть видео" in source["last_error"]


def test_unknown_source_config_error(client, settings):
    code = run_worker(
        "00000000-0000-0000-0000-000000000000", _client(client), settings, "mock",
        threading.Event(),
    )  # fmt: skip
    assert code == EXIT_CONFIG_ERROR


def test_stop_event_ends_episodes(client, settings, configured_source, monkeypatch):
    src = configured_source()
    stop = threading.Event()
    from app.pipeline import runner

    original = runner.Pipeline.process_frame

    def process_and_stop(self, frame):
        hits = original(self, frame)
        if frame.video_ts >= 6.0:  # посреди первого эпизода
            stop.set()
        return hits

    monkeypatch.setattr(runner.Pipeline, "process_frame", process_and_stop)
    assert run_worker(src, _client(client), settings, "mock", stop) == EXIT_OK
    (event,) = client.get("/api/v1/events").json()["items"]
    assert event["state"] == "ended" and event["video_ts_end"] <= 6.2


# --- heartbeat и восстановление -------------------------------------------------------------


def test_missing_heartbeat_marks_disconnected(app, client, hub, make_source):
    """Тест 7 (часть backend): нет heartbeat дольше таймаута -> disconnected."""
    fresh = make_source("Живой", status="running", last_heartbeat_at=utcnow())
    stale = make_source(
        "Пропал", status="running", last_heartbeat_at=utcnow() - timedelta(seconds=30)
    )
    never = make_source("Без heartbeat", status="running")
    with app.state.session_factory() as s:
        lost = source_service.check_heartbeats(s, hub, timeout_s=10)
    assert sorted(lost) == sorted([stale, never])
    assert client.get(f"/api/v1/sources/{fresh}").json()["status"] == "running"
    data = client.get(f"/api/v1/sources/{stale}").json()
    assert data["status"] == "disconnected" and "heartbeat" in data["last_error"]
    assert any(
        m["type"] == "source.status" and m["data"]["status"] == "disconnected" for m in hub.history
    )


def test_watchdog_runs_in_background(settings, make_source):
    settings.heartbeat_timeout_s = 0.6
    app = create_app(settings)
    with TestClient(app) as client:
        with app.state.session_factory() as s:
            s.add(Source(name="Пропал", kind="file", uri="x", analysis_fps=10,
                         status="running", last_heartbeat_at=utcnow()))  # fmt: skip
            s.commit()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if client.get("/api/v1/sources").json()[0]["status"] == "disconnected":
                break
            time.sleep(0.1)
        assert client.get("/api/v1/sources").json()[0]["status"] == "disconnected"


def test_recover_on_startup(settings, app, make_source, make_event):
    src = make_source(status="running")
    event_id = make_event(src, utcnow(), None)
    # «Перезапуск» backend: новое приложение на ту же БД.
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/v1/sources/{src}").json()["status"] == "stopped"
        event = client.get(f"/api/v1/events/{event_id}").json()
        assert event["state"] == "ended" and event["ended_at"] == event["last_seen_at"]


def test_start_images_kind_unsupported(client, make_source):
    src = make_source(kind="images")
    r = client.post(f"/api/v1/sources/{src}/start")
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNSUPPORTED_SOURCE_KIND"


def test_stop_without_process(client, make_source):
    idle = make_source()
    assert client.post(f"/api/v1/sources/{idle}/stop").json()["status"] == "idle"
    stale = make_source(status="running")
    assert client.post(f"/api/v1/sources/{stale}/stop").json()["status"] == "stopped"


# --- Сквозные тесты: настоящий процесс воркера --------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live(settings):
    """Backend на настоящем порту: воркер-подпроцесс ходит к нему по HTTP."""
    port = _free_port()
    settings.backend_url = f"http://127.0.0.1:{port}"
    settings.heartbeat_interval_s = 0.5
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    with TestClient(app, base_url=settings.backend_url) as client:
        yield app, client
    server.should_exit = True
    thread.join(timeout=15)


def _wait_status(client, source_id, statuses, timeout=60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        source = client.get(f"/api/v1/sources/{source_id}").json()
        if source["status"] in statuses:
            return source
        time.sleep(0.2)
    raise AssertionError(f"Статус не стал {statuses}: {source}")


def _setup_source(client, video: Path) -> str:
    src = client.post("/api/v1/sources/upload",
                      files={"file": ("shift.mp4", video.read_bytes(), "video/mp4")},
                      data={"name": "Камера 3", "site": "Цех №2"}).json()  # fmt: skip
    zone = client.post(f"/api/v1/sources/{src['id']}/zones",
                       json={"name": "Пресс", "polygon": DEFAULT_ZONE}).json()  # fmt: skip
    client.post("/api/v1/rules", json={"source_id": src["id"], "zone_id": zone["id"],
                                       "scenario_type": "danger_zone"})  # fmt: skip
    return src["id"]


def test_e2e_upload_start_events(live, settings, mock_video):
    """Тест 13: загрузка видео -> start (mock) -> события с двумя кадрами в журнале и на диске."""
    app, client = live
    src = _setup_source(client, mock_video)

    r = client.post(f"/api/v1/sources/{src}/start")
    assert r.status_code == 202 and r.json()["status"] == "running"
    assert app.state.worker_manager.wait(src, timeout=90)

    source = client.get(f"/api/v1/sources/{src}").json()
    assert source["status"] == "stopped", source["last_error"]
    page = client.get("/api/v1/events", params={"source_id": src}).json()
    assert page["total"] == 2
    for item in page["items"]:
        detail = client.get(f"/api/v1/events/{item['id']}").json()
        assert detail["state"] == "ended" and detail["notified_at"]
        assert sorted(e["kind"] for e in detail["evidence"]) == ["frame_annotated", "frame_raw"]
        for ev in detail["evidence"]:
            assert (settings.storage_dir / ev["url"].removeprefix("/media/")).stat().st_size > 0
            assert client.get(ev["url"]).headers["content-type"] == "image/jpeg"
    log = (settings.storage_dir / "logs" / f"{src}.log").read_text(encoding="utf-8")
    assert "Готово" in log


def test_e2e_double_start_and_stop(live, settings, mock_video):
    app, client = live
    settings.realtime_playback = True  # видео идёт ~26 с — успеваем остановить
    src = _setup_source(client, mock_video)
    assert client.post(f"/api/v1/sources/{src}/start").status_code == 202

    r = client.post(f"/api/v1/sources/{src}/start")
    assert r.status_code == 409 and r.json()["error"]["code"] == "SOURCE_ALREADY_RUNNING"

    # Ждём, пока воркер подтвердит первый эпизод (~3.8 с видео).
    deadline = time.monotonic() + 30
    while client.get("/api/v1/events").json()["total"] == 0:
        assert time.monotonic() < deadline, "эпизод не появился"
        time.sleep(0.2)

    t0 = time.monotonic()
    assert client.post(f"/api/v1/sources/{src}/stop").json()["status"] == "stopped"
    assert app.state.worker_manager.wait(src, timeout=20)
    assert time.monotonic() - t0 < 10  # штатная остановка, а не kill по таймауту

    assert client.get(f"/api/v1/sources/{src}").json()["status"] == "stopped"
    (event,) = client.get("/api/v1/events").json()["items"]
    assert event["state"] == "ended"
    with app.state.session_factory() as s:
        assert s.query(Event).filter_by(state="active").count() == 0


def test_e2e_worker_crash_sets_error(live, settings, mock_video):
    app, client = live
    settings.detector = "linq"
    src = _setup_source(client, mock_video)
    client.post(f"/api/v1/sources/{src}/start")
    assert app.state.worker_manager.wait(src, timeout=60)
    source = _wait_status(client, src, {"error"})
    assert "NotImplementedError" in source["last_error"]
