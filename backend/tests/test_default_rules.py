"""Этап B: правила по умолчанию, автозапуск, выбор сценариев зоны, изменения на лету."""

import uuid

from app.models import Rule
from app.pipeline.detectors.mock import MockDetector
from app.pipeline.runner import Pipeline
from app.pipeline.sources import NullFrameSource
from app.pipeline.worker import ConfigWatcher
from app.schemas.internal import SourceConfig
from app.services import rule_service
from tests.helpers import RecordingSink, make_config

ZONE = [[0.3, 0.3], [0.7, 0.3], [0.7, 0.9], [0.3, 0.9]]


def _upload(client, video):
    return client.post(
        "/api/v1/sources/upload",
        files={"file": ("shift.mp4", video.read_bytes(), "video/mp4")},
        data={"name": "Камера", "analysis_fps": "5"},
    )


def _rules(client, src):
    return client.get("/api/v1/rules", params={"source_id": src}).json()


def test_upload_creates_run_rule_and_starts_analysis(
    client, app, settings, sample_video, monkeypatch
):
    started = []
    monkeypatch.setattr(settings, "auto_start_on_upload", True)
    monkeypatch.setattr(
        app.state.worker_manager, "start", lambda db, source, **kw: started.append(source.id)
    )
    r = _upload(client, sample_video)
    assert r.status_code == 201, r.text
    src = r.json()["id"]
    (rule,) = _rules(client, src)
    assert rule["scenario_type"] == "run_fast_movement" and rule["zone_id"] is None
    assert rule["auto_created"] is True and rule["params"]["speed_threshold"] == 1.7
    assert started == [src]  # анализ запущен без ручных действий


def test_auto_start_failure_keeps_source_with_error(
    client, app, settings, sample_video, monkeypatch
):
    monkeypatch.setattr(settings, "auto_start_on_upload", True)

    def boom(db, source, **kw):
        raise RuntimeError("нет модели")

    monkeypatch.setattr(app.state.worker_manager, "start", boom)
    r = _upload(client, sample_video)
    assert r.status_code == 201
    src = client.get(f"/api/v1/sources/{r.json()['id']}").json()
    assert src["status"] == "error" and "нет модели" in src["last_error"]


def test_ensure_default_rules_is_idempotent_and_respects_deletion(client, app, make_source):
    src = make_source()
    with app.state.session_factory() as s:
        assert len(rule_service.ensure_default_rules(s, src)) == 1
        assert rule_service.ensure_default_rules(s, src) == []  # повтор — без дублей
    (rule,) = _rules(client, src)
    assert client.delete(f"/api/v1/rules/{rule['id']}").status_code == 204
    with app.state.session_factory() as s:
        assert rule_service.ensure_default_rules(s, src) == []  # удалённое не возвращается
    assert _rules(client, src) == []


def test_post_source_gets_default_rules(client):
    r = client.post("/api/v1/sources", json={"name": "Камера", "kind": "rtsp", "uri": "rtsp://x/1"})
    assert [x["scenario_type"] for x in _rules(client, r.json()["id"])] == ["run_fast_movement"]


def test_zone_scenarios_selection(client, make_source):
    src = make_source()
    # Без выбора — danger_zone, как раньше.
    z1 = client.post(f"/api/v1/sources/{src}/zones", json={"name": "Пресс", "polygon": ZONE}).json()
    assert z1["scenario_types"] == ["danger_zone"]
    # Явно пусто — зона без правил (например, только для разметки).
    z2 = client.post(
        f"/api/v1/sources/{src}/zones",
        json={"name": "Проход", "polygon": ZONE, "scenario_types": []},
    ).json()
    assert z2["scenario_types"] == []
    # Сценарий без зоны к зоне не привязывается.
    r = client.post(
        f"/api/v1/sources/{src}/zones",
        json={"name": "X", "polygon": ZONE, "scenario_types": ["run_fast_movement"]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "SCENARIO_NOT_ZONAL"
    assert len(client.get(f"/api/v1/sources/{src}/zones").json()) == 2  # неудачная зона не создана
    # Снять галочку — правило зоны удаляется; ничего не прислать — без изменений.
    z1 = client.patch(f"/api/v1/zones/{z1['id']}", json={"scenario_types": []}).json()
    assert z1["scenario_types"] == []
    z1 = client.patch(f"/api/v1/zones/{z1['id']}", json={"name": "Пресс 2"}).json()
    assert z1["scenario_types"] == [] and z1["name"] == "Пресс 2"
    assert [r["scenario_type"] for r in _rules(client, src)] == []


def test_rule_change_applies_to_running_pipeline():
    # Конвейер стартовал без правил; на 5-й секунде появилось правило danger_zone — эпизод есть.
    empty = make_config()
    empty = SourceConfig.model_validate({**empty.model_dump(mode="json"), "rules": []})
    with_rule = make_config(source_id=empty.source.id)
    with_rule = SourceConfig.model_validate(
        {**with_rule.model_dump(mode="json"), "zones": empty.model_dump(mode="json")["zones"],
         "rules": [{**with_rule.model_dump(mode="json")["rules"][0],
                    "zone_id": empty.zones[0].id}]}
    )  # fmt: skip
    sink = RecordingSink()
    pipeline = Pipeline(empty, MockDetector(empty.zones[0].polygon), sink)

    def frames():
        for f in NullFrameSource(empty.source.id, 14, 10, 1280, 720):
            if f.video_ts == 1.0:
                pipeline.request_config(with_rule)  # как ConfigWatcher из своего потока
            yield f

    pipeline.run(frames())
    (ep,) = sink.episodes()
    assert ep["scenario_type"] == "danger_zone" and ep["state"] == "ended"


def test_config_watcher_detects_changes_only():
    base = make_config()
    applied = []
    current = {"config": base}
    watcher = ConfigWatcher(lambda: current["config"], applied.append, base, 60)
    assert watcher.check() is False  # то же самое — не применяется
    changed = base.model_dump(mode="json")
    changed["rules"][0]["params"] = {"confirm_window_s": 3.0}
    current["config"] = SourceConfig.model_validate(changed)
    assert watcher.check() is True and applied[-1].rules[0].params == {"confirm_window_s": 3.0}
    status_only = changed | {"source": {**changed["source"], "status": "stopped"}}
    current["config"] = SourceConfig.model_validate(status_only)
    assert watcher.check() is False  # статус источника не влияет на анализ
    current["config"] = None  # backend недоступен — ошибка не роняет воркер

    def fail():
        raise ConnectionError("нет связи")

    watcher._load = fail
    assert watcher.check() is False


def test_default_rule_model_fields(app, make_source):
    src = make_source()
    with app.state.session_factory() as s:
        (rule,) = rule_service.ensure_default_rules(s, src)
        assert isinstance(rule, Rule) and rule.source_id == src and uuid.UUID(rule.id)
