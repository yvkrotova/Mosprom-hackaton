"""Публичный журнал: фильтры, пагинация, карточка события, действия оператора."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Evidence

H = timedelta(hours=1)
T = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)
URL = "/api/v1/events"


@pytest.fixture
def journal(make_source, make_zone, make_event):
    """Небольшой журнал: 2 камеры, 2 зоны, 2 типа сценариев, разные интервалы."""
    cam1, cam2 = make_source("Камера 1"), make_source("Камера 2")
    z1, z2 = make_zone(cam1, "Пресс"), make_zone(cam2, "Проход")
    ids = {
        # 08:00–08:10, камера 1, пресс
        "early": make_event(cam1, T, T + timedelta(minutes=10), zone_id=z1),
        # 09:50–10:20, пересекает 10:00
        "cross": make_event(cam1, T + 1.83 * H, T + 2.33 * H, zone_id=z1),
        # 10:30–10:40, камера 2, проход, другой сценарий
        "inside": make_event(
            cam2, T + 2.5 * H, T + 2.67 * H, zone_id=z2, scenario_type="fall_immobility"
        ),
        # с 11:00, ещё активен
        "ongoing": make_event(cam2, T + 3 * H, None, zone_id=z2, priority="critical"),
        # 13:00–13:05
        "late": make_event(cam1, T + 5 * H, T + 5.1 * H, zone_id=z1),
    }
    return {"cam1": cam1, "cam2": cam2, "z1": z1, "z2": z2, **ids}


def ids_of(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [e["id"] for e in resp.json()["items"]]


def test_default_sort_and_total(client, journal):
    body = client.get(URL).json()
    assert body["total"] == 5 and body["limit"] == 50 and body["offset"] == 0
    order = ["late", "ongoing", "inside", "cross", "early"]  # started_at по убыванию
    assert [e["id"] for e in body["items"]] == [journal[k] for k in order]
    first = body["items"][0]
    assert first["source_name"] == "Камера 1" and first["zone_name"] == "Пресс"
    assert first["started_at"].endswith("Z")


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"scenario_type": "fall_immobility"}, ["inside"]),
        ({"source_id": "cam1"}, ["late", "cross", "early"]),
        ({"zone_id": "z2"}, ["ongoing", "inside"]),
        ({"state": "active"}, ["ongoing"]),
        ({"priority": "critical"}, ["ongoing"]),
        ({"review_status": "confirmed"}, []),
    ],
)
def test_single_filters(client, journal, params, expected):
    params = {k: journal.get(v, v) for k, v in params.items()}
    assert ids_of(client.get(URL, params=params)) == [journal[k] for k in expected]


def test_time_interval_is_by_episode_start(client, journal):
    # Период — по началу эпизода (как в статистике). [10:00, 12:00]: inside (10:30) и ongoing
    # (11:00); cross начался в 09:50 — не входит, хотя ещё шёл в 10:00.
    params = {"from": "2026-10-23T10:00:00Z", "to": "2026-10-23T12:00:00Z"}
    assert ids_of(client.get(URL, params=params)) == [journal["ongoing"], journal["inside"]]
    # Только from: всё, что началось с 12:00.
    assert ids_of(client.get(URL, params={"from": "2026-10-23T12:00:00Z"})) == [journal["late"]]
    # Только to: всё, что началось до 09:00. Время без пояса — это UTC.
    assert ids_of(client.get(URL, params={"to": "2026-10-23T09:00:00"})) == [journal["early"]]


def test_combined_filters(client, journal):
    params = {
        "source_id": journal["cam1"],
        "zone_id": journal["z1"],
        "scenario_type": "danger_zone",
        "from": "2026-10-23T09:00:00Z",
        "to": "2026-10-23T12:00:00Z",
    }
    assert ids_of(client.get(URL, params=params)) == [journal["cross"]]  # началось в 09:50


def test_pagination(client, journal):
    page1 = client.get(URL, params={"limit": 2}).json()
    page2 = client.get(URL, params={"limit": 2, "offset": 2}).json()
    page3 = client.get(URL, params={"limit": 2, "offset": 4}).json()
    assert page1["total"] == page2["total"] == page3["total"] == 5
    seen = [e["id"] for p in (page1, page2, page3) for e in p["items"]]
    assert len(seen) == len(set(seen)) == 5
    assert client.get(URL, params={"offset": 10}).json()["items"] == []
    filtered = client.get(URL, params={"source_id": journal["cam1"], "limit": 1}).json()
    assert filtered["total"] == 3 and len(filtered["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"state": "paused"},
        {"from": "вчера"},
        {"from": "2026-10-23T12:00:00Z", "to": "2026-10-23T10:00:00Z"},
        {"from": "2026-10-23T12:00:00Z", "to": "2026-10-23T10:00:00"},  # to без пояса
    ],
)
def test_invalid_params_422(client, params):
    r = client.get(URL, params=params)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_event_detail(client, app, journal):
    event_id = journal["cross"]
    with app.state.session_factory() as s:
        s.add(Evidence(event_id=event_id, kind="frame_raw", path="p", url="/media/p", video_ts=1))
        s.commit()
    r = client.get(f"{URL}/{event_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == event_id and body["zone_name"] == "Пресс"
    assert body["actions"] == [] and len(body["evidence"]) == 1
    assert body["thumbnail_url"] == "/media/p"

    r = client.get(f"{URL}/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "EVENT_NOT_FOUND"


def _act(client, event_id, action, comment=None, author="Иванова А.С."):
    body = {"action": action, "author": author} | ({"comment": comment} if comment else {})
    return client.post(f"{URL}/{event_id}/actions", json=body)


def test_operator_actions(client, hub, journal):
    """Тест 5: действие меняет review_status, пишет историю, не меняет state."""
    event_id = journal["ongoing"]

    r = _act(client, event_id, "confirm", "Рабочий без допуска")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["review_status"] == "confirmed"
    assert body["state"] == "active"  # состояние эпизода не трогаем
    assert body["actions"][-1] | {"id": None, "created_at": None} == {
        "id": None,
        "event_id": event_id,
        "action": "confirm",
        "prev_status": "unreviewed",
        "new_status": "confirmed",
        "comment": "Рабочий без допуска",
        "author": "Иванова А.С.",
        "created_at": None,
    }

    r = _act(client, event_id, "comment", "Мастер смены уведомлён")
    assert r.json()["review_status"] == "confirmed"
    last = r.json()["actions"][-1]
    assert (last["prev_status"], last["new_status"]) == ("confirmed", "confirmed")

    assert _act(client, event_id, "escalate").json()["review_status"] == "escalated"
    assert _act(client, event_id, "mark_false").json()["review_status"] == "false_positive"
    assert _act(client, event_id, "reset").json()["review_status"] == "unreviewed"

    detail = client.get(f"{URL}/{event_id}").json()
    assert [a["action"] for a in detail["actions"]] == [
        "confirm",
        "comment",
        "escalate",
        "mark_false",
        "reset",
    ]
    assert detail["state"] == "active"

    reviewed = [m for m in hub.history if m["type"] == "event.reviewed"]
    assert len(reviewed) == 5
    assert "actions" not in reviewed[0]["data"]


def test_invalid_transition_409(client, journal):
    event_id = journal["early"]
    r = _act(client, event_id, "reset")  # unreviewed → reset недопустим
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "INVALID_TRANSITION"
    assert err["details"]["current_status"] == "unreviewed"
    assert set(err["details"]["allowed_from"]) == {"confirmed", "false_positive", "escalated"}

    _act(client, event_id, "mark_false")
    assert _act(client, event_id, "escalate").status_code == 409
    assert _act(client, event_id, "confirm").status_code == 409


def test_action_validation(client, journal):
    event_id = journal["early"]
    assert _act(client, event_id, "comment").status_code == 422  # без текста
    assert _act(client, event_id, "approve").status_code == 422
    assert _act(client, event_id, "confirm", author="").status_code == 422
    assert _act(client, str(uuid.uuid4()), "confirm").status_code == 404
