"""Зоны, правила и реестр сценариев."""

import uuid

import pytest

SQUARE = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]


def test_zone_crud(client, make_source):
    src = make_source()
    r = client.post(f"/api/v1/sources/{src}/zones", json={"name": "Пресс", "polygon": SQUARE})
    assert r.status_code == 201, r.text
    zone = r.json()
    assert zone["source_id"] == src and zone["polygon"] == SQUARE

    assert [z["id"] for z in client.get(f"/api/v1/sources/{src}/zones").json()] == [zone["id"]]
    tri = [[0.1, 0.1], [0.5, 0.1], [0.3, 0.6]]
    r = client.patch(f"/api/v1/zones/{zone['id']}", json={"polygon": tri})
    assert r.json()["polygon"] == tri and r.json()["name"] == "Пресс"

    assert client.delete(f"/api/v1/zones/{zone['id']}").status_code == 204
    assert client.get(f"/api/v1/sources/{src}/zones").json() == []
    r = client.get(f"/api/v1/sources/{uuid.uuid4()}/zones")
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


@pytest.mark.parametrize(
    "polygon",
    [
        [[0.1, 0.1], [0.5, 0.5]],  # меньше 3 точек
        [[0.1, 0.1], [1.5, 0.1], [0.3, 0.6]],  # вне 0..1
        [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]],  # нулевая площадь
        [[0.1], [0.2, 0.2], [0.3, 0.3]],  # не пары
    ],
)
def test_zone_polygon_validation(client, make_source, polygon):
    src = make_source()
    r = client.post(f"/api/v1/sources/{src}/zones", json={"name": "Z", "polygon": polygon})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_scenarios_list(client):
    scenarios = client.get("/api/v1/scenarios").json()
    # fall_immobility не готов и в список не попадает.
    assert [s["scenario_type"] for s in scenarios] == [
        "blocked_exit",
        "danger_zone",
        "run_fast_movement",
    ]
    by_type = {s["scenario_type"]: s for s in scenarios}
    dz = by_type["danger_zone"]
    assert dz["requires_zone"] is True
    assert dz["defaults"] == {
        "min_confidence": 0.4,
        "confirm_window_s": 1.0,
        "gap_tolerance_s": 2.0,
        "end_window_s": 2.0,  # по умолчанию = gap_tolerance_s: поведение danger_zone прежнее
        "labels": ["person"],
    }
    props = dz["params_schema"]["properties"]
    assert set(props) == set(dz["defaults"])
    assert props["min_confidence"]["maximum"] == 1

    run = by_type["run_fast_movement"]
    assert run["auto_enable"] is True and dz["auto_enable"] is False
    blocked = by_type["blocked_exit"]
    assert blocked["requires_zone"] is True and blocked["auto_enable"] is False
    assert (
        blocked["defaults"]["confirm_window_s"] == 10.0
        and blocked["defaults"]["end_window_s"] == 3.0
    )
    assert run["requires_zone"] is False
    assert run["defaults"]["speed_threshold"] == 1.7 and run["defaults"]["confirm_window_s"] == 0.6
    assert "ростов" in run["params_schema"]["properties"]["speed_threshold"]["description"]


@pytest.fixture
def setup(make_source, make_zone):
    src = make_source()
    return {"src": src, "zone": make_zone(src), "other_zone": make_zone(make_source("Другая"))}


def _create(client, **body):
    return client.post("/api/v1/rules", json={"scenario_type": "danger_zone"} | body)


def test_rule_create_fills_defaults(client, setup):
    r = _create(
        client, source_id=setup["src"], zone_id=setup["zone"], params={"confirm_window_s": 2}
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["params"] == {
        "min_confidence": 0.4,
        "confirm_window_s": 2.0,
        "gap_tolerance_s": 2.0,
        "end_window_s": 2.0,
        "labels": ["person"],
    }
    assert rule["version"] == 1 and rule["enabled"] is True and rule["priority"] == "high"
    assert (
        client.get("/api/v1/rules", params={"source_id": setup["src"]}).json()[0]["id"]
        == (rule["id"])
    )
    assert client.get("/api/v1/rules", params={"source_id": str(uuid.uuid4())}).json() == []


def test_rule_errors(client, setup, make_source):
    src = setup["src"]
    r = _create(client, source_id=src, scenario_type="fall_immobility")
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNKNOWN_SCENARIO"
    assert r.json()["error"]["details"]["available"] == [
        "blocked_exit",
        "danger_zone",
        "run_fast_movement",
    ]
    r = _create(client, source_id=src, scenario_type="smoking")
    assert r.json()["error"]["code"] == "UNKNOWN_SCENARIO"

    r = _create(client, source_id=src, params={"min_confidence": 2})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_SCENARIO_PARAMS"
    assert r.json()["error"]["details"]["errors"][0]["loc"] == ["params", "min_confidence"]
    r = _create(client, source_id=src, params={"unknown_param": 1})
    assert r.json()["error"]["code"] == "INVALID_SCENARIO_PARAMS"

    r = _create(client, source_id=src, zone_id=setup["other_zone"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "ZONE_SOURCE_MISMATCH"
    r = _create(client, source_id=src, zone_id=str(uuid.uuid4()))
    assert r.status_code == 404 and r.json()["error"]["code"] == "ZONE_NOT_FOUND"
    r = _create(client, source_id=str(uuid.uuid4()))
    assert r.status_code == 404 and r.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    r = _create(client, source_id=make_source("Без зон"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "ZONE_REQUIRED"

    # zone_id=null допустим, если у источника есть зоны: правило на все зоны.
    assert _create(client, source_id=src).status_code == 201


def test_rule_update_versions(client, setup):
    rule = _create(client, source_id=setup["src"], zone_id=setup["zone"]).json()
    url = f"/api/v1/rules/{rule['id']}"

    r = client.patch(url, json={"params": {"min_confidence": 0.6}})
    assert r.json()["version"] == 2
    assert r.json()["params"]["min_confidence"] == 0.6
    assert r.json()["params"]["confirm_window_s"] == 1.0  # остальные параметры сохранились

    assert client.patch(url, json={"priority": "critical"}).json()["version"] == 2
    assert client.patch(url, json={"params": {"min_confidence": 0.6}}).json()["version"] == 2
    assert client.patch(url, json={"enabled": False}).json()["version"] == 3
    assert client.patch(url, json={"enabled": False}).json()["version"] == 3

    r = client.patch(url, json={"zone_id": None})
    assert r.json()["zone_id"] is None
    r = client.patch(url, json={"zone_id": setup["other_zone"]})
    assert r.json()["error"]["code"] == "ZONE_SOURCE_MISMATCH"
    r = client.patch(url, json={"params": {"gap_tolerance_s": -1}})
    assert r.json()["error"]["code"] == "INVALID_SCENARIO_PARAMS"

    assert client.delete(url).status_code == 204
    assert client.patch(url, json={"enabled": True}).json()["error"]["code"] == "RULE_NOT_FOUND"


def test_deleting_zone_deletes_its_rules(client, setup):
    rule = _create(client, source_id=setup["src"], zone_id=setup["zone"]).json()
    assert client.delete(f"/api/v1/zones/{setup['zone']}").status_code == 204
    assert client.get("/api/v1/rules").json() == []
    assert client.delete(f"/api/v1/rules/{rule['id']}").status_code == 404
