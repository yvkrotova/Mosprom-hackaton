"""Общие фикстуры: у каждого теста своя БД SQLite и свой STORAGE_DIR."""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Event, Source, Zone
from tests.fixtures.make_sample_video import make_sample_video

INTERNAL_TOKEN = "test-internal-token"


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Короткое синтетическое видео 320x240, 10 кадр/с."""
    return make_sample_video(tmp_path_factory.mktemp("video") / "sample.mp4", seconds=3)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        storage_dir=tmp_path / "storage",
        internal_token=INTERNAL_TOKEN,
        detector="mock",
        # Фронтенд на «/» перехватил бы маршруты, которые тесты добавляют после создания app.
        frontend_dir=None,
        # Тесты загрузки не должны сами запускать воркеры; автозапуск проверяется отдельно.
        auto_start_on_upload=False,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # raise_server_exceptions=False: проверяем ответ 500, а не исключение в тесте.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": INTERNAL_TOKEN}


@pytest.fixture
def hub(app: FastAPI):
    return app.state.hub


@pytest.fixture
def make_source(app: FastAPI, client: TestClient):
    """Создаёт источник прямо в БД (не зависит от публичного API)."""

    def _make(name: str = "Камера 1", **kwargs) -> str:
        fields = {"kind": "file", "uri": "video.mp4", "analysis_fps": 10.0} | kwargs
        with app.state.session_factory() as s:
            src = Source(name=name, **fields)
            s.add(src)
            s.commit()
            return src.id

    return _make


@pytest.fixture
def make_event(app: FastAPI, client: TestClient):
    """Создаёт событие в БД. started/ended — datetime (UTC); ended=None — эпизод активен."""

    def _make(source_id: str, started: datetime, ended: datetime | None = None, **kwargs) -> str:
        fields = {
            "scenario_type": "danger_zone",
            "zone_id": None,
            "track_id": 1,
            "priority": "high",
            "state": "ended" if ended else "active",
            "confidence": 0.9,
            "video_ts_start": 0.0,
            "video_ts_end": 1.0,
            "video_ts_detected": 1.0,
            "model_version": "mock-1.0",
            "last_seen_at": ended or started,
            "detected_at": started,
        } | kwargs
        with app.state.session_factory() as s:
            event = Event(source_id=source_id, started_at=started, ended_at=ended, **fields)
            s.add(event)
            s.commit()
            return event.id

    return _make


@pytest.fixture
def make_zone(app: FastAPI, client: TestClient):
    def _make(source_id: str, name: str = "Зона", polygon=None) -> str:
        polygon = polygon or [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]]
        with app.state.session_factory() as s:
            zone = Zone(source_id=source_id, name=name, polygon=polygon)
            s.add(zone)
            s.commit()
            return zone.id

    return _make
