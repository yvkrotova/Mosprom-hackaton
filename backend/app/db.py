"""Подключение к БД: engine, сессии, базовый класс моделей, миграции."""

from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BACKEND_DIR


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str, *, foreign_keys: bool = True) -> Engine:
    """foreign_keys=False — только для миграций: SQLite меняет таблицы пересозданием
    (CREATE new → копия → DROP old), и со включёнными внешними ключами DROP старой таблицы каскадно
    удалил бы зоны, правила, кадры и историю, которые ссылаются на неё (ON DELETE CASCADE)."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_engine(database_url, pool_pre_ping=True)

    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        # Иначе SQLite игнорирует ON DELETE CASCADE. Для миграций — выключены (см. выше).
        cur.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cur.execute("PRAGMA journal_mode=WAL")  # backend и чтение не блокируют друг друга
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def run_migrations(database_url: str) -> None:
    """alembic upgrade head для указанной БД (используется при старте backend и в тестах)."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(cfg, "head")


def get_db(request: Request) -> Iterator[Session]:
    """FastAPI-зависимость: сессия на запрос."""
    with request.app.state.session_factory() as session:
        yield session
