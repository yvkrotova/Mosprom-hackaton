"""Живой просмотр, позы MockDetector, фильтр /stats по камере, раздача фронтенда."""

import json
import threading
import uuid
from datetime import UTC, datetime

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import BACKEND_DIR
from app.main import create_app
from app.pipeline.backend_client import BackendClient
from app.pipeline.detectors.mock import MockDetector
from app.pipeline.interfaces import Frame
from app.pipeline.worker import run_worker
from app.services.worker_manager import WorkerManager
from tests.conftest import INTERNAL_TOKEN
from tests.test_worker import configured_source, mock_video  # noqa: F401 — фикстуры

META = {
    "frame_idx": 12, "video_ts": 1.2, "width": 1280, "height": 720, "fps": 9.9,
    "tracks": [{"track_id": 1, "label": "person", "confidence": 0.9, "bbox": [0.4, 0.3, 0.5, 0.9],
                "keypoints": None, "state": "in_zone", "zone_ids": ["z"]}],
}  # fmt: skip


def _jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.zeros((36, 64, 3), np.uint8))
    return buf.tobytes()


def _post_live(client, source_id, meta=META, image=True, headers=None):
    return client.post(
        f"/internal/live/{source_id}",
        data={"metadata": json.dumps(meta)},
        files={"file": ("f.jpg", _jpeg(), "image/jpeg")} if image else None,
        headers={"X-Internal-Token": INTERNAL_TOKEN} if headers is None else headers,
    )


def test_live_frame_reaches_websocket(client, make_source):
    src = make_source()
    with client.websocket_connect(f"/ws/live/{src}") as ws:
        assert _post_live(client, src).status_code == 204
        msg = ws.receive_json()
    assert msg["type"] == "live.frame"
    data = msg["data"]
    assert data["source_id"] == src and data["video_ts"] == 1.2
    assert data["image"].startswith("data:image/jpeg;base64,")
    assert data["tracks"][0]["state"] == "in_zone"

    r = client.get(f"/api/v1/sources/{src}/live.jpg")
    assert r.status_code == 200 and r.content == _jpeg()


def test_new_subscriber_gets_latest_frame_and_end(client, app, make_source):
    src = make_source()
    _post_live(client, src, image=False)  # кадр без картинки (replay без видео)
    with client.websocket_connect(f"/ws/live/{src}") as ws:
        first = ws.receive_json()
        assert first["type"] == "live.frame" and first["data"]["image"] is None
        app.state.live_hub.end(src)
        assert ws.receive_json()["type"] == "live.ended"
    r = client.get(f"/api/v1/sources/{src}/live.jpg")
    assert r.status_code == 404 and r.json()["error"]["code"] == "LIVE_NOT_AVAILABLE"


def test_live_frame_errors(client, make_source):
    src = make_source()
    assert _post_live(client, src, headers={}).status_code == 401
    r = _post_live(client, src, meta={**META, "width": 0})
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"]
    r = _post_live(client, str(uuid.uuid4()))
    assert r.status_code == 404 and r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_worker_streams_live_states(client, app, settings, configured_source, monkeypatch):  # noqa: F811
    src = configured_source()
    seen = []
    original = app.state.live_hub.publish

    def recording_publish(sid, meta, jpeg):
        seen.append((meta, jpeg))
        original(sid, meta, jpeg)

    monkeypatch.setattr(app.state.live_hub, "publish", recording_publish)
    # Без ограничения частоты: прогон без реального времени длится доли секунды.
    monkeypatch.setattr(settings, "tracks_push_hz", 0)
    client_ = BackendClient("http://testserver", INTERNAL_TOKEN, http=client, retries=0)
    assert run_worker(src, client_, settings, "mock", threading.Event()) == 0

    assert seen, "воркер не прислал ни одного живого кадра"
    states = {t["state"] for meta, _ in seen for t in meta["tracks"]}
    assert {"normal", "in_zone", "alert"} <= states
    track = next(t for meta, _ in seen for t in meta["tracks"])
    assert len(track["keypoints"]) == 17
    assert track["track_state"] in ("confirmed", "lost") and isinstance(track["flags"], list)
    assert seen[0][0]["detector_kind"] == "mock"
    flags = {f for meta, _ in seen for t in meta["tracks"] for f in t["flags"]}
    assert {"in_zone", "violation"} <= flags
    assert all(jpeg for _, jpeg in seen)


def test_mock_keypoints_follow_bbox():
    det = MockDetector().infer(Frame("s", 30, 3.0, 1280, 720))
    (person,) = det.detections
    x1, y1, x2, y2 = person.bbox
    assert len(person.keypoints) == 17
    for x, y, conf in person.keypoints:
        assert x1 - 0.02 <= x <= x2 + 0.02 and y1 <= y <= y2 and 0.8 <= conf <= 1.0
    # Лодыжки — у нижней кромки рамки (точка контроля «ноги»).
    assert person.keypoints[15][1] == pytest.approx(y2, abs=0.02)


def test_start_realtime_flag(settings, app):
    manager: WorkerManager = app.state.worker_manager
    assert "--realtime" in manager._command("id", realtime=True)
    assert "--realtime" not in manager._command("id", realtime=False)


def test_stats_filter_by_source(client, make_source, make_event):
    a, b = make_source("A"), make_source("B")
    t = datetime(2026, 10, 23, 9, tzinfo=UTC)
    make_event(a, t, t)
    make_event(a, t, t)
    make_event(b, t, t)
    assert client.get("/api/v1/stats").json()["total"] == 3
    body = client.get("/api/v1/stats", params={"source_id": a}).json()
    assert body["total"] == 2 and [s["key"] for s in body["by_source"]] == [a]


def test_frontend_is_served(settings):
    settings.frontend_dir = BACKEND_DIR.parent / "frontend"
    with TestClient(create_app(settings)) as client:
        _check_frontend(client)


def _check_frontend(client):
    r = client.get("/")
    assert r.status_code == 200 and "LinQ Видеоконтроль" in r.text
    font = client.get("/fonts/inter-cyrillic-wght-normal.woff2")
    assert "font/woff2" in font.headers["content-type"]
    js = client.get("/js/app.js")
    assert "javascript" in js.headers["content-type"]
    assert js.headers["cache-control"] == "no-cache"  # после обновления кода — без Ctrl+F5
    # API и его единый формат ошибок фронтенд не перекрывает.
    r = client.get("/api/v1/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/api/v1/health").status_code == 200
