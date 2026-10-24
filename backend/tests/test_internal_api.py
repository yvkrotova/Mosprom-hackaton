"""Внутренний API воркера: идемпотентный upsert событий, доказательства, heartbeat, конфиг."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Event, Rule
from app.schemas.enums import ReviewStatus

T0 = datetime(2026, 10, 23, 9, 15, tzinfo=UTC)


def episode(source_id: str, zone_id: str | None = None, **overrides) -> dict:
    body = {
        "source_id": source_id,
        "scenario_type": "danger_zone",
        "zone_id": zone_id,
        "track_id": 1,
        "priority": "high",
        "state": "active",
        "confidence": 0.8,
        "started_at": T0.isoformat(),
        "last_seen_at": (T0 + timedelta(seconds=1)).isoformat(),
        "video_ts_start": 12.0,
        "video_ts_end": 13.0,
        "video_ts_detected": 13.0,
        "detected_at": (T0 + timedelta(seconds=1.2)).isoformat(),
        "model_version": "mock-1.0",
        "rule_id": None,
        "rule_version": None,
        "description": "Человек в зоне",
    }
    return body | overrides


def types(hub) -> list[str]:
    return [m["type"] for m in hub.history if m["type"].startswith("event.")]


def count_events(app) -> int:
    with app.state.session_factory() as s:
        return s.query(Event).count()


@pytest.fixture
def put(client, internal_headers):
    def _put(event_id: str, body: dict):
        return client.put(f"/internal/events/{event_id}", json=body, headers=internal_headers)

    return _put


def test_repeated_put_creates_one_event_and_one_alert(app, hub, put, make_source, make_zone):
    """Тест 1: повторный PUT одного события — одна запись и одно event.created."""
    src = make_source()
    body = episode(src, make_zone(src))
    event_id = str(uuid.uuid4())

    r1 = put(event_id, body)
    assert r1.status_code == 201 and r1.json()["outcome"] == "created"
    for _ in range(3):
        r = put(event_id, body)
        assert r.status_code == 200 and r.json()["outcome"] == "unchanged"

    assert count_events(app) == 1
    assert types(hub) == ["event.created"]
    with app.state.session_factory() as s:
        ev = s.get(Event, event_id)
        assert ev.received_at is not None and ev.notified_at is not None
        assert ev.alerts_sent == 1


def test_long_episode_lifecycle(app, hub, put, make_source, make_zone):
    """Тест 2: 1 created, N updated, 1 ended, одна запись."""
    src = make_source()
    zone = make_zone(src)
    event_id = str(uuid.uuid4())
    put(event_id, episode(src, zone))
    for i in range(1, 4):
        r = put(
            event_id,
            episode(
                src,
                zone,
                last_seen_at=(T0 + timedelta(seconds=1 + i)).isoformat(),
                video_ts_end=13.0 + i,
                confidence=0.8 + i * 0.03,
            ),
        )
        assert r.json()["outcome"] == "updated"
    end = episode(
        src,
        zone,
        state="ended",
        last_seen_at=(T0 + timedelta(seconds=4)).isoformat(),
        ended_at=(T0 + timedelta(seconds=4)).isoformat(),
        video_ts_end=16.0,
        confidence=0.5,  # ниже максимума — confidence не должен уменьшиться
    )
    assert put(event_id, end).json()["outcome"] == "ended"
    assert put(event_id, end).json()["outcome"] == "unchanged"

    assert types(hub) == ["event.created"] + ["event.updated"] * 3 + ["event.ended"]
    assert count_events(app) == 1
    last = hub.history[-1]["data"]
    assert last["state"] == "ended" and last["ended_at"].endswith("Z")
    assert last["confidence"] == pytest.approx(0.89)
    assert last["review_status"] == "unreviewed"


def test_ended_back_to_active_is_409(put, make_source):
    """Тест 3: ended → active → 409 EPISODE_ALREADY_ENDED."""
    src = make_source()
    event_id = str(uuid.uuid4())
    put(event_id, episode(src, state="ended", ended_at=T0.isoformat()))
    r = put(event_id, episode(src, video_ts_end=14.0))  # продолжение после конца
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EPISODE_ALREADY_ENDED"


def test_replay_rerun_of_ended_episode_is_unchanged(app, put, make_source):
    """Повторный прогон той же записи (детерминированный event_id) проходит уже известный
    отрезок: событие не меняется, повторной тревоги нет."""
    src = make_source()
    event_id = str(uuid.uuid4())
    put(event_id, episode(src, state="ended", ended_at=T0.isoformat()))
    later = T0 + timedelta(hours=1)  # второй прогон — другое настенное время
    r = put(event_id, episode(src, started_at=later.isoformat(), video_ts_end=12.5))
    assert r.status_code == 200 and r.json()["outcome"] == "unchanged"
    r = put(event_id, episode(src, state="ended", ended_at=later.isoformat()))
    assert r.json()["outcome"] == "unchanged"
    with app.state.session_factory() as s:
        event = s.get(Event, event_id)
        assert event.alerts_sent == 1 and event.state == "ended"


def test_upsert_never_touches_review_status(app, put, make_source):
    src = make_source()
    event_id = str(uuid.uuid4())
    put(event_id, episode(src))
    with app.state.session_factory() as s:
        s.get(Event, event_id).review_status = ReviewStatus.CONFIRMED
        s.commit()
    put(event_id, episode(src, video_ts_end=20.0))
    with app.state.session_factory() as s:
        assert s.get(Event, event_id).review_status == ReviewStatus.CONFIRMED


def test_upsert_validation(put, make_source):
    src = make_source()
    assert put("not-a-uuid", episode(src)).json()["error"]["code"] == "VALIDATION_ERROR"
    r = put(str(uuid.uuid4()), episode(str(uuid.uuid4())))
    assert r.status_code == 404 and r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    r = put(str(uuid.uuid4()), episode(src, state="ended"))  # нет ended_at
    assert r.status_code == 422


def test_internal_requires_token(client, make_source):
    src = make_source()
    for headers in ({}, {"X-Internal-Token": "wrong"}):
        r = client.put(f"/internal/events/{uuid.uuid4()}", json=episode(src), headers=headers)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"
    assert client.get(f"/internal/sources/{src}/config").status_code == 401


def _upload(client, headers, event_id, kind="frame_raw", video_ts=13.0, data=b"\xff\xd8jpeg"):
    meta = {
        "kind": kind,
        "video_ts": video_ts,
        "frame_time": T0.isoformat(),
        "bboxes": [
            {"label": "person", "confidence": 0.9, "bbox": [0.4, 0.3, 0.5, 0.9], "track_id": 1}
        ],
    }
    return client.post(
        f"/internal/events/{event_id}/evidence",
        files={"file": ("frame.jpg", data, "image/jpeg")},
        data={"metadata": json.dumps(meta)},
        headers=headers,
    )


def test_evidence_saved_once(client, app, hub, settings, put, internal_headers, make_source):
    src = make_source()
    event_id = str(uuid.uuid4())
    put(event_id, episode(src))

    r1 = _upload(client, internal_headers, event_id)
    assert r1.status_code == 201, r1.text
    ev = r1.json()
    assert ev["url"] == f"/media/evidence/{event_id}/frame_raw_13.000.jpg"
    assert (settings.storage_dir / "evidence" / event_id / "frame_raw_13.000.jpg").read_bytes()

    r2 = _upload(client, internal_headers, event_id)
    assert r2.status_code == 200 and r2.json()["id"] == ev["id"]
    assert _upload(client, internal_headers, event_id, kind="frame_annotated").status_code == 201

    # Файл отдаётся по /media, событие показывает кадр-превью.
    assert client.get(ev["url"]).content == b"\xff\xd8jpeg"
    data = hub.history[-1]["data"]
    assert len(data["evidence"]) == 2
    assert data["thumbnail_url"].endswith("frame_annotated_13.000.jpg")
    assert types(hub) == ["event.created", "event.updated", "event.updated"]


def test_evidence_errors(client, put, internal_headers, make_source):
    src = make_source()
    assert _upload(client, internal_headers, str(uuid.uuid4())).status_code == 404
    event_id = str(uuid.uuid4())
    put(event_id, episode(src))
    r = client.post(
        f"/internal/events/{event_id}/evidence",
        files={"file": ("x.jpg", b"x", "image/jpeg")},
        data={"metadata": '{"kind": "photo"}'},
        headers=internal_headers,
    )
    assert r.status_code == 422 and r.json()["error"]["details"]["errors"]
    r = client.post(
        f"/internal/events/{event_id}/evidence",
        files={"file": ("x.exe", b"x", "application/octet-stream")},
        data={"metadata": '{"kind": "frame_raw", "video_ts": 1}'},
        headers=internal_headers,
    )
    assert r.status_code == 415 and r.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def _heartbeat(client, headers, source_id, status="running", **kw):
    body = {
        "source_id": source_id,
        "status": status,
        "fps": 9.8,
        "inference_ms_avg": 12.0,
        "inference_ms_p95": 15.0,
        "frames_processed": 100,
        "model_version": "mock-1.0",
        "detector_kind": "mock",
    } | kw
    return client.post("/internal/heartbeat", json=body, headers=headers)


def test_heartbeat_updates_status_and_stats(client, app, hub, internal_headers, make_source):
    src = make_source()
    r = _heartbeat(client, internal_headers, src)
    assert r.status_code == 200
    assert r.json()["status"] == "running" and r.json()["last_heartbeat_at"].endswith("Z")
    _heartbeat(client, internal_headers, src)  # тот же статус — без WS
    r = _heartbeat(client, internal_headers, src, status="error", error="LinQ недоступен")
    assert r.json()["status"] == "error" and r.json()["last_error"] == "LinQ недоступен"

    statuses = [m["data"] for m in hub.history if m["type"] == "source.status"]
    assert [(d["prev_status"], d["status"]) for d in statuses] == [
        ("idle", "running"),
        ("running", "error"),
    ]
    from app.models import ProcessingStat

    with app.state.session_factory() as s:
        assert s.query(ProcessingStat).filter_by(source_id=src).count() == 3


def test_late_running_heartbeat_does_not_restart_stopped(
    client, app, internal_headers, make_source, monkeypatch
):
    src = make_source(status="stopped")
    # Backend уже попросил воркер остановиться — запоздавший running игнорируется.
    monkeypatch.setattr(app.state.worker_manager, "is_stopping", lambda source_id: True)
    assert _heartbeat(client, internal_headers, src).json()["status"] == "stopped"


def test_manual_worker_heartbeat_marks_source_running(client, internal_headers, make_source):
    # Воркер, запущенный вручную, «оживляет» остановленный источник.
    src = make_source(status="stopped")
    assert _heartbeat(client, internal_headers, src).json()["status"] == "running"


def test_source_config(client, app, internal_headers, make_source, make_zone):
    src = make_source()
    zone = make_zone(src)
    other = make_source("Другая")
    make_zone(other)
    with app.state.session_factory() as s:
        s.add_all(
            [
                Rule(source_id=src, zone_id=zone, scenario_type="danger_zone", params={"x": 1}),
                Rule(source_id=src, zone_id=None, scenario_type="danger_zone", enabled=False),
            ]
        )
        s.commit()
    r = client.get(f"/internal/sources/{src}/config", headers=internal_headers)
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["source"]["id"] == src
    assert [z["id"] for z in cfg["zones"]] == [zone]
    assert len(cfg["rules"]) == 1 and cfg["rules"][0]["params"] == {"x": 1}
    assert cfg["settings"] == {"episode_update_interval_s": 1.0, "heartbeat_interval_s": 2.0}
    r = client.get(f"/internal/sources/{uuid.uuid4()}/config", headers=internal_headers)
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_websocket_receives_alert(client, put, make_source):
    src = make_source()
    with client.websocket_connect("/ws/events") as ws:
        put(str(uuid.uuid4()), episode(src))
        msg = ws.receive_json()
    assert msg["type"] == "event.created"
    assert msg["ts"].endswith("Z")
    assert msg["data"]["source_name"] == "Камера 1"
    assert msg["data"]["notified_at"] is not None
    assert "actions" not in msg["data"]


def test_websocket_disconnected_clients_are_dropped(client, hub, put, make_source):
    src = make_source()
    with client.websocket_connect("/ws/events"):
        assert _wait(lambda: hub.client_count == 1)
    assert _wait(lambda: hub.client_count == 0)
    r = put(str(uuid.uuid4()), episode(src))  # рассылка без клиентов — без ошибок
    assert r.status_code == 201


def _wait(cond, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()
