"""Экстренный вызов по событию; смены руководителей и рейтинг безопасности."""

from datetime import UTC, datetime, timedelta

from app.models import Event

T = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # в прошлом: рейтинг не считает будущее
H = timedelta(hours=1)


def _status(app, event_id, status):
    with app.state.session_factory() as s:
        s.get(Event, event_id).review_status = status
        s.commit()


def test_emergency_call_is_recorded_and_broadcast(client, hub, make_source, make_event):
    src = make_source()
    ev = make_event(src, T, priority="medium")
    contacts = client.get("/api/v1/emergency/contacts").json()
    assert [c["number"] for c in contacts] == ["112", "101", "103"]
    body = {"number": "112", "author": "Иванова А.С.", "comment": "Человек под стеллажом"}
    r = client.post(f"/api/v1/events/{ev}/emergency", json=body)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["emergency_service"] == "112 — Единая служба спасения"
    assert detail["emergency_by"] == "Иванова А.С." and detail["priority"] == "critical"
    assert detail["review_status"] == "unreviewed"  # вызов — не проверка
    (action,) = detail["actions"]
    assert action["action"] == "emergency" and "Человек под стеллажом" in action["comment"]
    first_at = detail["emergency_at"]
    again = client.post(f"/api/v1/events/{ev}/emergency", json={"number": "103", "author": "Б"})
    assert again.json()["emergency_at"] == first_at and len(again.json()["actions"]) == 2
    assert "event.emergency" in [m["type"] for m in hub.history]
    bad = client.post(f"/api/v1/events/{ev}/emergency", json={"number": "999", "author": "Б"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "UNKNOWN_EMERGENCY_SERVICE"
    via_actions = client.post(
        f"/api/v1/events/{ev}/actions", json={"action": "emergency", "author": "Б"}
    )
    assert via_actions.status_code == 422


def test_shift_overlap_rules(client):
    end = (T + 8 * H).isoformat()
    body = {"supervisor": "Петров", "site": "Склад", "started_at": T.isoformat(), "ended_at": end}
    a = client.post("/api/v1/shifts", json=body)
    assert a.status_code == 201, a.text
    other_site = client.post("/api/v1/shifts", json={"supervisor": "Сидоров", "site": "Цех",
                                                     "started_at": T.isoformat()})  # fmt: skip
    assert other_site.status_code == 201
    clash = client.post("/api/v1/shifts", json={"supervisor": "Иванов", "site": "Склад",
                                                "started_at": (T + H).isoformat()})  # fmt: skip
    assert clash.status_code == 409 and "Петров" in clash.json()["error"]["message"]
    all_sites = client.post("/api/v1/shifts", json={"supervisor": "Иванов",
                                                    "started_at": (T + H).isoformat()})  # fmt: skip
    assert all_sites.status_code == 409
    ended = client.post(f"/api/v1/shifts/{other_site.json()['id']}/end").json()
    assert ended["ended_at"] is not None
    bad = client.post("/api/v1/shifts", json={"supervisor": "Х", "started_at": T.isoformat(),
                                              "ended_at": (T - H).isoformat()})  # fmt: skip
    assert bad.status_code == 422


def test_rating_per_8_hours_excludes_false_positives(app, client, make_source, make_event):
    sklad = make_source("Камера склада", site="Склад")
    ceh = make_source("Камера цеха", site="Цех")
    shift = lambda who, site, start, hours: client.post("/api/v1/shifts", json={  # noqa: E731
        "supervisor": who, "site": site, "started_at": start.isoformat(),
        "ended_at": (start + hours * H).isoformat()}).json()  # fmt: skip
    shift("Петров", "Склад", T, 8)
    shift("Сидоров", "Цех", T, 4)
    shift("Петров", "Склад", T + 24 * H, 8)
    for minute in (10, 20, 30):
        make_event(sklad, T + timedelta(minutes=minute))  # 3 за первую смену Петрова
    false_ev = make_event(sklad, T + 2 * H)
    _status(app, false_ev, "false_positive")
    confirmed = make_event(ceh, T + H)
    _status(app, confirmed, "confirmed")
    make_event(ceh, T + 6 * H)  # после смены Сидорова — не его
    make_event(sklad, T + 12 * H)  # между сменами — ничьё
    rows = client.get("/api/v1/shifts/rating", params={
        "from": (T - H).isoformat(), "to": (T + 40 * H).isoformat()}).json()  # fmt: skip
    by = {r["supervisor"]: r for r in rows}
    assert by["Петров"]["hours"] == 16 and by["Петров"]["shifts"] == 2
    assert by["Петров"]["incidents"] == 3 and by["Петров"]["false_positive"] == 1
    assert by["Петров"]["per_shift"] == 1.5  # 3 на 16 ч → 1,5 на 8 ч
    assert by["Сидоров"]["incidents"] == 1 and by["Сидоров"]["confirmed"] == 1
    assert by["Сидоров"]["per_shift"] == 2.0  # 1 на 4 ч → 2 на 8 ч
    assert [r["supervisor"] for r in rows] == ["Петров", "Сидоров"] and rows[0]["rank"] == 1
    period = {"from": (T - H).isoformat(), "to": (T + 40 * H).isoformat(), "site": "Цех"}
    only_ceh = client.get("/api/v1/shifts/rating", params=period).json()
    assert [r["supervisor"] for r in only_ceh] == ["Сидоров"]


def test_one_click_feedback(client, hub, make_source, make_event, app):
    ev = make_event(make_source(), T)
    url = f"/api/v1/events/{ev}/feedback"
    r = client.post(url, json={"verdict": "false_positive", "author": "Иванова"})
    assert r.status_code == 200 and r.json()["review_status"] == "false_positive"
    # Ложное → подтверждено одним действием, без промежуточного сброса.
    r = client.post(url, json={"verdict": "confirmed", "author": "Иванова"})
    detail = r.json()
    assert detail["review_status"] == "confirmed"
    assert [(a["action"], a["prev_status"], a["new_status"]) for a in detail["actions"]] == [
        ("mark_false", "unreviewed", "false_positive"),
        ("confirm", "false_positive", "confirmed"),
    ]
    again = client.post(url, json={"verdict": "confirmed", "author": "Иванова"}).json()
    assert len(again["actions"]) == 2  # повтор той же оценки ничего не меняет
    _status(app, ev, "escalated")
    kept = client.post(url, json={"verdict": "confirmed", "author": "Иванова"}).json()
    assert kept["review_status"] == "escalated"  # «передано мастеру» уже подтверждено
    assert "event.reviewed" in [m["type"] for m in hub.history]
    bad = client.post(url, json={"verdict": "maybe", "author": "Иванова"})
    assert bad.status_code == 422
