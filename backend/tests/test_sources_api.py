"""Источники: CRUD, загрузка видео, превью."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

URL = "/api/v1/sources"


def _upload(client, video: Path | None = None, name="Камера 3", content=None, filename=None):
    data = content if content is not None else video.read_bytes()
    return client.post(
        f"{URL}/upload",
        files={"file": (filename or "shift.mp4", data, "video/mp4")},
        data={"name": name, "site": "Цех №2", "analysis_fps": "5"},
    )


def test_source_crud(client):
    r = client.post(URL, json={"name": "Камера 1", "kind": "rtsp", "uri": "rtsp://10.0.0.5/s1"})
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["status"] == "idle" and src["analysis_fps"] == 10.0  # дефолт из настроек
    assert src["created_at"].endswith("Z")

    assert client.get(URL).json()[0]["id"] == src["id"]
    assert client.get(f"{URL}/{src['id']}").json()["name"] == "Камера 1"

    r = client.patch(f"{URL}/{src['id']}", json={"name": "Камера 1А", "site": "Склад"})
    assert r.json()["name"] == "Камера 1А" and r.json()["site"] == "Склад"

    assert client.delete(f"{URL}/{src['id']}").status_code == 204
    r = client.get(f"{URL}/{src['id']}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_source_validation(client):
    assert client.post(URL, json={"name": "", "uri": "x"}).status_code == 422
    assert client.post(URL, json={"name": "a", "uri": "x", "kind": "usb"}).status_code == 422
    assert client.post(URL, json={"name": "a", "uri": "x", "extra": 1}).status_code == 422
    assert client.patch(f"{URL}/{uuid.uuid4()}", json={"name": "a"}).status_code == 404


def test_running_source_cannot_be_deleted_or_repointed(client, make_source):
    src = make_source(status="running")
    r = client.delete(f"{URL}/{src}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "SOURCE_RUNNING"
    r = client.patch(f"{URL}/{src}", json={"uri": "other.mp4"})
    assert r.status_code == 409
    assert client.patch(f"{URL}/{src}", json={"name": "Новое имя"}).status_code == 200


def test_delete_source_keeps_journal(client, make_source, make_zone, make_event):
    src = make_source()
    zone = make_zone(src)
    event_id = make_event(src, datetime(2026, 10, 23, tzinfo=UTC), zone_id=zone)
    assert client.delete(f"{URL}/{src}").status_code == 204
    event = client.get(f"/api/v1/events/{event_id}").json()
    assert event["source_name"] is None and event["zone_name"] is None


def test_upload_and_preview(client, settings, sample_video):
    r = _upload(client, sample_video)
    assert r.status_code == 201, r.text
    src = r.json()
    assert src["kind"] == "file" and src["site"] == "Цех №2" and src["analysis_fps"] == 5.0
    stored = Path(src["uri"])
    assert stored.parent == settings.videos_dir.resolve() and stored.exists()

    r = client.get(f"{URL}/{src['id']}/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (240, 320)  # исходное разрешение

    # Удаление источника удаляет и загруженный файл.
    assert client.delete(f"{URL}/{src['id']}").status_code == 204
    assert not stored.exists()


def test_upload_rejects_bad_files(client, settings):
    r = _upload(client, content=b"hello", filename="notes.txt")
    assert r.status_code == 415 and r.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
    r = _upload(client, content=b"not really a video", filename="fake.mp4")
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_VIDEO"
    assert list(settings.videos_dir.iterdir()) == []  # мусор не остаётся
    r = client.post(f"{URL}/upload", files={"file": ("a.mp4", b"x", "video/mp4")})
    assert r.status_code == 422  # нет name


def test_upload_size_limit(client, settings, sample_video):
    settings.max_upload_mb = 0
    r = _upload(client, sample_video)
    assert r.status_code == 413 and r.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert list(settings.videos_dir.iterdir()) == []


def test_preview_unavailable(client, make_source):
    src = make_source(uri="missing/video.mp4")
    r = client.get(f"{URL}/{src}/preview")
    assert r.status_code == 422 and r.json()["error"]["code"] == "PREVIEW_UNAVAILABLE"
