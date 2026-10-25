"""API экранов оператора: просмотр, фильтры, CSV, статистика, замеры, видео эпизода, разметка."""

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Event, ProcessingStat, Source
from app.models.base import utcnow
from app.pipeline.geometry import polygon_problems

T = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]


def _set(app, model, obj_id, **fields):
    with app.state.session_factory() as s:
        obj = s.get(model, obj_id)
        for k, v in fields.items():
            setattr(obj, k, v)
        s.commit()


# --- просмотр ---------------------------------------------------------------------------------


def test_view_is_idempotent_and_separate_from_review(client, make_source, make_event):
    src = make_source(site="Склад")
    other = make_source("Камера 2", site="Цех")
    a = make_event(src, T)
    make_event(src, T + timedelta(minutes=1))
    make_event(other, T)
    counts = client.get("/api/v1/events/unviewed").json()
    assert counts["total"] == 3
    first = client.post(f"/api/v1/events/{a}/view").json()
    again = client.post(f"/api/v1/events/{a}/view").json()
    assert first["viewed_at"] and again["viewed_at"] == first["viewed_at"]
    assert again["review_status"] == "unreviewed"  # просмотр — не проверка
    assert client.get("/api/v1/events", params={"viewed": False}).json()["total"] == 2
    assert client.get("/api/v1/events", params={"viewed": True}).json()["total"] == 1
    by_site = client.get("/api/v1/events/unviewed", params={"site": "Склад"}).json()
    assert by_site == {"total": 1, "by_source": [{"source_id": src, "count": 1}]}
    assert client.post("/api/v1/events/nope/view").status_code == 404


# --- фильтры и сортировка ---------------------------------------------------------------------


def test_order_zone_none_site_and_confirmed_includes_escalated(
    app, client, make_source, make_zone, make_event
):
    src = make_source(site="Склад")
    zone = make_zone(src)
    frame_ev = make_event(src, T, T + timedelta(seconds=5))
    zone_ev = make_event(src, T + timedelta(hours=1), zone_id=zone)
    _set(app, Event, frame_ev, review_status="escalated")
    _set(app, Event, zone_ev, review_status="confirmed")
    ids = lambda r: [e["id"] for e in r.json()["items"]]  # noqa: E731
    assert ids(client.get("/api/v1/events", params={"order": "asc"})) == [frame_ev, zone_ev]
    assert ids(client.get("/api/v1/events")) == [zone_ev, frame_ev]
    assert ids(client.get("/api/v1/events", params={"zone_id": "none"})) == [frame_ev]
    assert ids(client.get("/api/v1/events", params={"site": "Склад", "zone_id": zone})) == [zone_ev]
    assert client.get("/api/v1/events", params={"site": "Другой"}).json()["total"] == 0
    confirmed = client.get("/api/v1/events", params={"review_status": "confirmed"})
    assert sorted(ids(confirmed)) == sorted([frame_ev, zone_ev])
    escalated = client.get("/api/v1/events", params={"review_status": "escalated"})
    assert ids(escalated) == [frame_ev]


# --- CSV --------------------------------------------------------------------------------------


def test_csv_exports_whole_selection_with_filters(app, client, make_source, make_zone, make_event):
    src = make_source("Камера «Склад»")
    zone = make_zone(src, "Проход")
    for i in range(60):  # больше одной страницы журнала (limit по умолчанию 50)
        make_event(src, T + timedelta(minutes=i), T + timedelta(minutes=i, seconds=12),
                   video_ts_end=12.5, zone_id=zone if i % 2 else None)  # fmt: skip
    escalated = make_event(src, T - timedelta(hours=1), T, confidence=0.913)
    _set(app, Event, escalated, review_status="escalated")
    r = client.get("/api/v1/events/export.csv", params={"tz_offset_min": 180, "order": "asc"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][0].startswith("Начало") and len(rows) == 1 + 61
    first = rows[1]
    assert first[0] == "2026-10-24 11:00:00 +03:00"  # 08:00 UTC → местное +03:00
    assert first[2] == "Весь кадр" and first[5] == "91"
    assert first[7] == "Подтверждено, передано мастеру"
    assert rows[2][4] == "12,5" and rows[3][2] == "Проход"
    only_zone = client.get("/api/v1/events/export.csv", params={"zone_id": zone})
    assert len(only_zone.content.decode("utf-8-sig").strip().splitlines()) == 1 + 30


# --- статистика -------------------------------------------------------------------------------


def test_stats_summary_invariant_zone_time_and_demo(app, client, make_source, make_event):
    src = make_source()
    states = ["unreviewed", "confirmed", "escalated", "false_positive", "unreviewed"]
    for i, status in enumerate(states):
        ended = None if i == 4 else T + timedelta(minutes=i, seconds=20)
        ev = make_event(src, T + timedelta(minutes=i), ended, video_ts_start=0.0,
                        video_ts_end=10.0 * (i + 1), alerts_sent=1)  # fmt: skip
        _set(app, Event, ev, review_status=status)
    make_event(src, T, T + timedelta(seconds=3), scenario_type="run_fast_movement",
               video_ts_end=99.0, alerts_sent=1)  # fmt: skip
    make_event(src, T - timedelta(days=2), T - timedelta(days=2), alerts_sent=1)  # вне периода
    params = {"from": T.isoformat(), "to": (T + timedelta(hours=1)).isoformat()}
    s = client.get("/api/v1/stats", params=params).json()["summary"]
    assert s["total"] == 6 and s["active"] == 1
    assert s["unreviewed"] + s["confirmed"] + s["false_positive"] == s["total"]
    assert (s["confirmed"], s["escalated"], s["false_positive"]) == (2, 1, 1)
    assert s["avg_zone_time_s"] == 25.0  # только завершённые danger_zone: 10, 20, 30, 40
    assert s["alerts_per_episode"] == 1.0
    stats = client.get("/api/v1/stats", params=params).json()
    assert stats["demo"] == {
        "is_demo": True,
        "reason": "события получены заглушкой детектора (mock)",
    }
    empty = client.get("/api/v1/stats", params={"from": "2030-01-01T00:00:00Z"}).json()
    assert empty["summary"]["avg_zone_time_s"] is None
    assert empty["summary"]["alerts_per_episode"] is None and empty["demo"]["is_demo"] is False


# --- замеры -----------------------------------------------------------------------------------


def test_metrics_states_and_replay_fields(app, client, make_source):
    idle = make_source("Не запускалась")
    replay = make_source("Replay")
    now = utcnow()
    _set(app, Source, replay, status="stopped", run_started_at=now - timedelta(minutes=1))
    with app.state.session_factory() as s:
        s.add(ProcessingStat(source_id=replay, ts=now, fps=55.0, inference_ms_avg=None,
                             recorded_inference_ms_avg=41.0, recorded_inference_ms_p50=40.0,
                             recorded_inference_ms_p95=47.5, frame_ms_avg=3.2, frames_processed=30,
                             detector_kind="linq_tpu_replay", replay_file="te1.jsonl"))  # fmt: skip
        s.commit()
    items = {m["source_id"]: m for m in client.get("/api/v1/metrics").json()["sources"]}
    assert items[idle]["measurement_state"] == "not_started" and items[idle]["fps"] is None
    m = items[replay]
    assert m["measurement_state"] == "finished" and m["detector_kind"] == "linq_tpu_replay"
    assert m["inference_ms_avg"] is None and m["recorded_inference_ms_p95"] == 47.5
    assert m["latency"]["events_count"] == 0 and m["run_started_at"]
    _set(app, Source, replay, status="running", run_started_at=now + timedelta(seconds=1))
    pending = client.get("/api/v1/metrics", params={"source_id": replay}).json()["sources"][0]
    assert pending["measurement_state"] == "pending"


# --- видео эпизода ----------------------------------------------------------------------------


def test_episode_video_url_for_uploaded_file_and_note_for_stream(client, make_source, make_event):
    r = client.post(
        "/api/v1/sources/upload",
        files={"file": ("v.mp4", _tiny_video(), "video/mp4")},
        data={"name": "Файл"},
    )
    src = r.json()
    ev = make_event(src["id"], T, T + timedelta(seconds=4), video_ts_start=3.0, video_ts_end=7.0)
    event = client.get(f"/api/v1/events/{ev}").json()
    assert event["video_url"] == f"/media/videos/{src['id']}.mp4" and event["video_note"] is None
    assert client.get(event["video_url"]).status_code == 200  # файл реально отдаётся
    stream = make_source("Поток", kind="rtsp", uri="rtsp://cam/1")
    ev2 = client.get(f"/api/v1/events/{make_event(stream, T)}").json()
    assert ev2["video_url"] is None and "Поток" in ev2["video_note"]


def _tiny_video() -> bytes:
    import tempfile
    from pathlib import Path

    from tests.test_replay import make_video

    with tempfile.TemporaryDirectory() as tmp:
        return make_video(Path(tmp) / "t.mp4", seconds=0.5).read_bytes()


# --- контур и разметка ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("polygon", "problem"),
    [
        ([[0.1, 0.1], [0.5, 0.1]], "минимум 3 точки"),
        ([[0.1, 0.1], [0.1, 0.1], [0.5, 0.5]], "3 разные точки"),
        ([[0.1, 0.1], [0.3, 0.3], [0.5, 0.5]], "площадь"),
        ([[0.1, 0.1], [0.5, 0.5], [0.5, 0.1], [0.1, 0.5]], "самопересекается"),
        ([[0.1, 0.1], [1.2, 0.1], [0.5, 0.5]], "внутри кадра"),
    ],
)
def test_polygon_problems(polygon, problem):
    assert any(problem in p for p in polygon_problems(polygon))
    assert polygon_problems(SQUARE) == []


def test_zone_api_rejects_self_intersecting_polygon(client, make_source):
    src = make_source()
    bow = [[0.1, 0.1], [0.5, 0.5], [0.5, 0.1], [0.1, 0.5]]
    r = client.post(f"/api/v1/sources/{src}/zones", json={"name": "Х", "polygon": bow})
    assert r.status_code == 422 and "самопересекается" in r.text


def test_layout_saved_atomically(client, make_source):
    src = make_source()
    put = lambda body: client.put(f"/api/v1/sources/{src}/layout", json=body)  # noqa: E731
    r = put({
        "zones": [
            {"name": "Опасная зона", "polygon": SQUARE, "scenario_types": ["danger_zone"]},
            {"name": "Проход", "polygon": [[0.6, 0.6], [0.9, 0.6], [0.9, 0.9]],
             "scenario_types": ["blocked_exit"], "enabled": False},
        ],
        "frame_rules": [{"scenario_type": "run_fast_movement", "enabled": False}],
    })  # fmt: skip
    assert r.status_code == 200, r.text
    zones = {z["name"]: z for z in r.json()["zones"]}
    assert zones["Опасная зона"]["enabled"] and not zones["Проход"]["enabled"]
    assert zones["Проход"]["scenario_types"] == ["blocked_exit"]
    run = [x for x in r.json()["rules"] if x["scenario_type"] == "run_fast_movement"]
    assert len(run) == 1 and run[0]["enabled"] is False and run[0]["zone_id"] is None

    # Черновик с ошибкой: ничего не меняется, ошибки — с индексом зоны и полем.
    keep = zones["Опасная зона"]["id"]
    bad = put({
        "zones": [
            {"id": keep, "name": "Переименована", "polygon": SQUARE,
             "scenario_types": ["danger_zone"]},
            {"name": " ", "polygon": [[0.1, 0.1], [0.5, 0.5], [0.5, 0.1], [0.1, 0.5]],
             "scenario_types": []},
        ],
    })  # fmt: skip
    assert bad.status_code == 422
    errors = bad.json()["error"]["details"]["errors"]
    assert {(e["zone_index"], e["field"]) for e in errors} == {
        (1, "name"),
        (1, "polygon"),
        (1, "scenario_types"),
    }
    names = sorted(z["name"] for z in client.get(f"/api/v1/sources/{src}/zones").json())
    assert names == ["Опасная зона", "Проход"]

    # Удаление зоны и переименование — одной операцией; история событий не трогается.
    ok = put({"zones": [{"id": keep, "name": "Переименована", "polygon": SQUARE,
                         "scenario_types": ["danger_zone"]}]})  # fmt: skip
    assert [z["name"] for z in ok.json()["zones"]] == ["Переименована"]
    assert [z["id"] for z in ok.json()["zones"]] == [keep]


def test_clear_journal_and_view_all(app, client, settings, make_source, make_event):
    src = make_source(site="Склад")
    other = make_source("Камера 2", site="Цех")
    a = make_event(src, T)
    make_event(other, T)
    evidence = settings.storage_dir / "evidence" / a / "frame.jpg"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"jpg")
    from app.models import Evidence

    with app.state.session_factory() as s:
        s.add(Evidence(event_id=a, kind="frame_annotated", path=f"evidence/{a}/frame.jpg",
                       url=f"/media/evidence/{a}/frame.jpg"))  # fmt: skip
        s.commit()
    # «Очистить» ленту участка: только просмотр, события остаются.
    assert client.post("/api/v1/events/view-all", params={"site": "Склад"}).json() == {"viewed": 1}
    assert client.get("/api/v1/events/unviewed").json()["total"] == 1
    assert client.get("/api/v1/events").json()["total"] == 2
    # «Очистить всё» в журнале: события, история и файлы кадров удалены.
    assert client.delete("/api/v1/events").json() == {"deleted": 2}
    assert client.get("/api/v1/events").json()["total"] == 0
    assert not evidence.exists() and not evidence.parent.exists()


def test_deleted_events_are_not_resurrected_by_running_analysis(
    client, internal_headers, make_source, make_event
):
    from tests.test_internal_api import episode

    src = make_source()
    one = make_event(src, T)
    two = make_event(src, T + timedelta(minutes=1))
    assert client.delete(f"/api/v1/events/{one}").status_code == 204
    assert client.get(f"/api/v1/events/{one}").status_code == 404
    assert client.get("/api/v1/events").json()["total"] == 1
    # Воркер ещё присылает обновление удалённого эпизода — событие не появляется снова.
    r = client.put(f"/internal/events/{one}", json=episode(src), headers=internal_headers)
    assert r.status_code == 200 and r.json()["outcome"] == "deleted"
    assert client.delete("/api/v1/events").json() == {"deleted": 1}
    r = client.put(f"/internal/events/{two}", json=episode(src), headers=internal_headers)
    assert r.json()["outcome"] == "deleted"
    assert client.get("/api/v1/events").json()["total"] == 0
    assert client.delete(f"/api/v1/events/{one}").status_code == 404
