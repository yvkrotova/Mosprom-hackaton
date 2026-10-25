"""Миграции, соответствие моделей схеме и сохранение данных после перезапуска."""

from datetime import UTC, datetime

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select

import app.models  # noqa: F401
from app.db import Base, make_engine, make_session_factory, run_migrations
from app.models import Event, Evidence, OperatorAction, Rule, Source, Zone
from app.schemas.enums import EpisodeState, ReviewStatus, SourceStatus


def _migrated_engine(settings):
    run_migrations(settings.database_url)
    return make_engine(settings.database_url)


def test_migrations_match_models(settings):
    engine = _migrated_engine(settings)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == [], f"Модели расходятся с миграциями: {diff}"


def test_required_event_indexes(settings):
    engine = _migrated_engine(settings)
    indexed = {tuple(ix["column_names"]) for ix in inspect(engine).get_indexes("events")}
    engine.dispose()
    for col in ("scenario_type", "source_id", "zone_id", "started_at", "review_status", "state"):
        assert (col,) in indexed, col


def _make_event(source_id: str, zone_id: str) -> Event:
    now = datetime(2026, 10, 23, 9, 15, tzinfo=UTC)
    return Event(
        scenario_type="danger_zone",
        source_id=source_id,
        zone_id=zone_id,
        track_id=1,
        priority="high",
        state=EpisodeState.ACTIVE,
        confidence=0.9,
        started_at=now,
        last_seen_at=now,
        video_ts_start=12.0,
        video_ts_end=13.0,
        video_ts_detected=13.0,
        detected_at=now,
        model_version="mock-1.0",
    )


def test_data_survives_restart(settings):
    """Тест 6: новый engine на тот же файл SQLite видит все данные."""
    engine = _migrated_engine(settings)
    with make_session_factory(engine)() as s:
        src = Source(name="Камера 1", kind="file", uri="v.mp4", analysis_fps=10)
        zone = Zone(source=src, name="Пресс", polygon=[[0, 0], [1, 0], [1, 1]])
        s.add_all([src, zone, Rule(source=src, scenario_type="danger_zone", params={"a": 1})])
        s.flush()
        event = _make_event(src.id, zone.id)
        event.evidence.append(Evidence(kind="frame_raw", path="p", url="/media/p", video_ts=13.0))
        event.actions.append(
            OperatorAction(
                action="confirm", prev_status="unreviewed", new_status="confirmed", author="Иванова"
            )
        )
        s.add(event)
        s.commit()
        source_id, event_id = src.id, event.id
    engine.dispose()

    # «Перезапуск»: новый engine, миграции повторно (должны быть no-op).
    engine = _migrated_engine(settings)
    with make_session_factory(engine)() as s:
        src = s.get(Source, source_id)
        assert src.status == SourceStatus.IDLE
        assert src.zones[0].polygon == [[0, 0], [1, 0], [1, 1]]
        assert src.rules[0].params == {"a": 1} and src.rules[0].version == 1
        ev = s.get(Event, event_id)
        assert ev.review_status == ReviewStatus.UNREVIEWED
        assert ev.alerts_sent == 0
        assert [e.kind for e in ev.evidence] == ["frame_raw"]
        assert ev.actions[0].author == "Иванова"
    engine.dispose()


def test_delete_source_cascades_config_but_keeps_journal(settings):
    engine = _migrated_engine(settings)
    with make_session_factory(engine)() as s:
        src = Source(name="Камера", kind="file", uri="v.mp4", analysis_fps=10)
        zone = Zone(source=src, name="Z", polygon=[[0, 0], [1, 0], [1, 1]])
        s.add_all([src, zone, Rule(source=src, scenario_type="danger_zone")])
        s.flush()
        s.add(_make_event(src.id, zone.id))
        s.commit()
        s.delete(src)
        s.commit()
        assert s.scalars(select(Zone)).all() == []
        assert s.scalars(select(Rule)).all() == []
        assert len(s.scalars(select(Event)).all()) == 1
    engine.dispose()


def test_upgrade_keeps_related_rows(settings):
    """SQLite меняет таблицы пересозданием: миграции не должны каскадно удалять зоны, правила,
    замеры, кадры и историю (так было бы со включёнными внешними ключами)."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from app.config import BACKEND_DIR

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "0005")
    engine = make_engine(settings.database_url)
    now = "2026-10-23 09:00:00"
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO sources (id, name, kind, uri, status, analysis_fps, created_at, "
                "updated_at) "
                f"VALUES ('s1', 'Камера', 'file', 'v.mp4', 'idle', 10, '{now}', '{now}')"
            )
        )
        c.execute(
            text(
                "INSERT INTO zones (id, source_id, name, polygon, created_at, updated_at) "
                f"VALUES ('z1', 's1', 'Зона', '[[0,0],[1,0],[1,1]]', '{now}', '{now}')"
            )
        )
        c.execute(
            text(
                "INSERT INTO rules (id, source_id, zone_id, scenario_type, enabled, priority, "
                "params, version, auto_created, created_at, updated_at) "
                "VALUES ('r1', 's1', 'z1', 'danger_zone', 1, 'high', '{}', 1, 0, "
                f"'{now}', '{now}')"
            )
        )
        c.execute(
            text(
                "INSERT INTO processing_stats (id, source_id, ts, frames_processed) "
                f"VALUES ('p1', 's1', '{now}', 5)"
            )
        )
    engine.dispose()
    run_migrations(settings.database_url)  # 0005 → head (с пересозданием sources/events/shifts)
    engine = make_engine(settings.database_url)
    with engine.connect() as c:
        counts = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                  for t in ("sources", "zones", "rules", "processing_stats")}  # fmt: skip
    engine.dispose()
    assert counts == {"sources": 1, "zones": 1, "rules": 1, "processing_stats": 1}
