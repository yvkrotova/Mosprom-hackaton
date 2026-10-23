"""Сводка /stats, замеры /metrics и /health."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import OperationalError

from app.models import ProcessingStat
from app.services import source_service

T = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
M = timedelta(minutes=1)


@pytest.fixture
def journal(make_source, make_zone, make_event):
    cam1, cam2 = make_source("Камера 1"), make_source("Камера 2")
    z1, z2 = make_zone(cam1, "Пресс"), make_zone(cam2, "Проход")
    make_event(cam1, T + 5 * M, T + 6 * M, zone_id=z1, review_status="confirmed")
    make_event(cam1, T + 20 * M, T + 21 * M, zone_id=z1, review_status="false_positive")
    make_event(cam1, T + 70 * M, None, zone_id=z1)  # 10:10, активен
    make_event(cam2, T + 75 * M, T + 76 * M, zone_id=z2, scenario_type="fall_immobility",
               review_status="escalated")  # fmt: skip
    return {"cam1": cam1, "cam2": cam2, "z1": z1, "z2": z2}


def test_stats_all_time(client, journal):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from"] is None and body["to"] is None and body["total"] == 4
    assert body["by_scenario_type"] == [
        {"key": "danger_zone", "label": None, "count": 3},
        {"key": "fall_immobility", "label": None, "count": 1},
    ]
    assert body["by_source"] == [
        {"key": journal["cam1"], "label": "Камера 1", "count": 3},
        {"key": journal["cam2"], "label": "Камера 2", "count": 1},
    ]
    assert [(z["label"], z["count"]) for z in body["by_zone"]] == [("Пресс", 3), ("Проход", 1)]
    assert body["by_hour"] == [
        {"hour": "2026-10-23T09:00:00Z", "count": 2},
        {"hour": "2026-10-23T10:00:00Z", "count": 2},
    ]
    assert body["review"] == {
        "unreviewed": 1, "confirmed": 1, "false_positive": 1, "escalated": 1,
    }  # fmt: skip


def test_stats_interval(client, journal):
    body = client.get(
        "/api/v1/stats", params={"from": "2026-10-23T09:10:00Z", "to": "2026-10-23T10:12:00Z"}
    ).json()
    # 09:20 и активное с 10:10 пересекаются с интервалом; 09:05–09:06 и 10:15 — нет.
    assert body["from"] == "2026-10-23T09:10:00Z" and body["total"] == 2
    assert body["review"]["false_positive"] == 1 and body["review"]["unreviewed"] == 1
    r = client.get("/api/v1/stats", params={"from": "2026-10-24T00:00:00Z", "to": "2026-10-23"})
    assert r.status_code == 422


def test_stats_empty(client):
    body = client.get("/api/v1/stats").json()
    assert body["total"] == 0 and body["by_hour"] == [] and body["review"]["confirmed"] == 0


def test_metrics(client, app, make_source, make_event):
    src, other = make_source("Камера 1"), make_source("Камера 2")
    with app.state.session_factory() as s:
        s.add_all([
            ProcessingStat(source_id=src, ts=T, fps=8.0, inference_ms_avg=30.0,
                           inference_ms_p95=40.0, frames_processed=100, detector_kind="mock",
                           model_version="mock-1.0"),
            ProcessingStat(source_id=src, ts=T + M, fps=24.6, inference_ms_avg=18.4,
                           inference_ms_p95=22.9, frames_processed=1830, detector_kind="linq",
                           model_version="yolo-linq"),
        ])  # fmt: skip
        s.commit()
    for i, (real_s, video_s, notify_ms) in enumerate([(1.2, 1.0, 5.0), (1.4, 1.1, 15.0)]):
        started = T + i * M
        make_event(
            src, started, started + M,
            video_ts_start=10.0, video_ts_detected=10.0 + video_s,
            detected_at=started + timedelta(seconds=real_s),
            notified_at=started + timedelta(seconds=real_s, milliseconds=notify_ms),
            alerts_sent=1,
        )  # fmt: skip

    body = client.get("/api/v1/metrics", params={"source_id": src}).json()
    (m,) = body["sources"]
    assert m["fps"] == 24.6 and m["frames_processed"] == 1830  # последний замер
    assert m["detector_kind"] == "linq" and m["ts"] == "2026-10-23T09:01:00Z"
    lat = body["latency"]
    assert lat["events_count"] == 2
    assert lat["detection_delay_real_s_avg"] == pytest.approx(1.3)
    assert lat["detection_delay_video_ts_s_avg"] == pytest.approx(1.05)
    assert lat["notify_delay_ms_avg"] == pytest.approx(10.0)
    assert lat["notify_delay_ms_p95"] == pytest.approx(14.5)
    assert lat["alerts_per_episode"] == 1.0

    everything = client.get("/api/v1/metrics").json()
    assert len(everything["sources"]) == 2
    assert everything["sources"][1]["source_id"] == other
    assert everything["sources"][1]["fps"] is None  # ещё не запускался


def test_metrics_without_events(client):
    lat = client.get("/api/v1/metrics").json()["latency"]
    assert lat["events_count"] == 0 and lat["alerts_per_episode"] is None


def test_health_ok(client, make_source):
    make_source(status="running")
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok" and body["db"] == "ok" and body["detector_kind"] == "mock"
    assert body["sources"][0]["status"] == "running"


def test_health_shows_disconnected(client, app, make_source):
    """Тест 7: нет heartbeat дольше таймаута -> источник disconnected в /health."""
    src = make_source(status="running", last_heartbeat_at=T)
    with app.state.session_factory() as s:
        source_service.check_heartbeats(s, app.state.hub, timeout_s=10)
    body = client.get("/api/v1/health").json()
    assert body["status"] == "degraded"
    (h,) = body["sources"]
    assert h["id"] == src and h["status"] == "disconnected" and h["last_error"]
    assert h["last_heartbeat_at"] == "2026-10-23T09:00:00Z"


def test_health_shows_worker_error(client, internal_headers, make_source):
    """Тест 12: heartbeat status=error -> источник error в /health."""
    src = make_source()
    client.post("/internal/heartbeat", headers=internal_headers,
                json={"source_id": src, "status": "error", "error": "LinQ: таймаут"})  # fmt: skip
    body = client.get("/api/v1/health").json()
    assert body["status"] == "degraded"
    assert body["sources"][0]["status"] == "error"
    assert body["sources"][0]["last_error"] == "LinQ: таймаут"


def test_health_db_error(client, app, monkeypatch):
    def broken():
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(app.state, "session_factory", broken)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "db": "error", "detector_kind": "mock",
                        "sources": []}  # fmt: skip
