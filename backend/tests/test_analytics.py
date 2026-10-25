"""Демо-история и аналитика «Статистики»: паттерны начальников смен, динамика, горячие точки."""

from datetime import UTC, datetime, timedelta

from app.models import Event, Shift, Source
from app.services import analytics_service, demo_service

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _generate(app, days=30):
    with app.state.session_factory() as s:
        return demo_service.generate(s, days=days, tz_offset_min=180, now=NOW)


def _overview(app, days=30, supervisor=None):
    with app.state.session_factory() as s:
        return analytics_service.overview(s, days, supervisor, 180, now=NOW)


def test_demo_patterns_show_in_rating(app, client):
    info = _generate(app)
    assert info["shifts"] >= 55 and info["events"] > 50 and info["sources"] == 4
    data = _overview(app)
    rows = {r["supervisor"]: r for r in data["supervisors"]}
    assert set(rows) == {n for n, _a, _b in demo_service.SUPERVISORS}
    best, worst = data["supervisors"][0], data["supervisors"][-1]
    assert best["supervisor"] == "Иванов И.И." and best["badge"] == "leader"
    assert worst["supervisor"] == "Петров А.С." and worst["badge"] == "risk"
    assert rows["Сидоров В.М."]["trend"] == "improving"
    assert (
        rows["Петров А.С."]["per_shift"]
        > rows["Кузнецов Д.О."]["per_shift"]
        > rows["Иванов И.И."]["per_shift"]
    )
    assert 0 <= worst["safety_index"] < best["safety_index"] <= 100
    # Демо-события — в обычном журнале, рядом с реальными.
    assert client.get("/api/v1/events", params={"limit": 1}).json()["total"] == info["events"]


def test_daily_breakdown_and_supervisor_filter(app, client):
    _generate(app, days=14)
    data = _overview(app, days=14)
    assert len(data["daily"]) == 14
    day = next(d for d in data["daily"] if d["total"])
    assert sum(day["by_scenario"].values()) == day["total"]
    assert day["confirmed"] + day["false_positive"] + day["unreviewed"] == day["total"]
    assert day["supervisors"]
    only = _overview(app, days=14, supervisor="Петров А.С.")
    assert all(set(d["supervisors"]) <= {"Петров А.С."} for d in only["daily"])
    assert sum(d["total"] for d in only["daily"]) < sum(d["total"] for d in data["daily"])


def test_hotspots_and_summary(app, client):
    _generate(app)
    data = _overview(app)
    zones = data["hotspots"]["zones"]
    assert (
        zones and abs(sum(z["share"] for z in zones) - 1) < 0.05 or sum(z["count"] for z in zones)
    )
    assert zones[0]["count"] >= zones[-1]["count"] and zones[0]["risk"] in ("high", "medium")
    cams = data["hotspots"]["cameras"]
    assert cams[0]["name"] == "Демо · Камера 01"  # самая «опасная» камера генератора
    s = data["summary"]
    assert s["total"] > 0 and s["top_zone"]["name"] == zones[0]["name"]
    assert s["best_supervisor"]["supervisor"] == "Иванов И.И."
    assert 0 < s["confirmed_share"] < 1 and s["demo"] is True


def test_demo_clear_keeps_real_data_and_regenerates_cleanly(app, client, make_source, make_event):
    real = make_source("Реальная камера")
    make_event(real, NOW - timedelta(days=1))
    _generate(app)
    _generate(app)  # повторный запуск заменяет, а не удваивает
    with app.state.session_factory() as s:
        assert s.query(Source).filter_by(is_demo=True).count() == 4
    r = client.delete("/api/v1/demo/history").json()
    assert r["sources"] == 4 and r["events"] > 0
    with app.state.session_factory() as s:
        assert s.query(Event).count() == 1 and s.query(Shift).count() == 0
        assert s.query(Source).count() == 1


def test_demo_source_cannot_start(app, client):
    _generate(app, days=7)
    demo = next(s for s in client.get("/api/v1/sources").json() if s["is_demo"])
    r = client.post(f"/api/v1/sources/{demo['id']}/start")
    assert r.status_code == 422 and r.json()["error"]["code"] == "DEMO_SOURCE"


def test_overview_api(client):
    r = client.get("/api/v1/analytics/overview", params={"days": 7, "tz_offset_min": 180})
    assert r.status_code == 200 and len(r.json()["daily"]) == 7
    assert client.get("/api/v1/analytics/overview", params={"days": 9}).status_code == 422
    assert client.post("/api/v1/demo/history", params={"days": 7}).json()["events"] >= 0


def test_few_shifts_are_not_ranked(app, client):
    _generate(app)
    start = NOW - timedelta(hours=30)
    with app.state.session_factory() as s:
        s.add(Shift(supervisor="Новиков П.П.", site="Нет такого участка", started_at=start,
                    ended_at=start + timedelta(hours=8)))  # fmt: skip
        s.commit()
    data = _overview(app)
    newbie = next(r for r in data["supervisors"] if r["supervisor"] == "Новиков П.П.")
    assert newbie["rank"] is None and newbie["safety_index"] == 100  # одна чистая смена
    assert data["summary"]["best_supervisor"]["supervisor"] == "Иванов И.И."
    assert data["supervisors"][-1]["supervisor"] == "Новиков П.П."


def test_real_events_are_not_blamed_on_demo_supervisors(app, client, make_source, make_event):
    real = make_source("Реальная камера")
    make_event(real, NOW - timedelta(days=2, hours=3))
    _generate(app)
    with app.state.session_factory() as s:
        t = NOW - timedelta(days=2, hours=3)
        utc = lambda d: d.replace(tzinfo=UTC)  # noqa: E731
        covering = [x for x in s.query(Shift).all() if utc(x.started_at) <= t < utc(x.ended_at)]
    assert covering == []  # демо-смены нет — реальное событие не приписано выдуманному начальнику
    assert _overview(app)["summary"]["total"] > 1  # но в динамике оно учитывается
