"""Окружение Alembic: адрес БД из настроек приложения, метаданные из app.models."""

from alembic import context
from sqlalchemy import Engine

import app.models  # noqa: F401 — регистрирует все таблицы в Base.metadata
from app.config import get_settings
from app.db import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    # run_migrations() передаёт адрес явно; из CLI (alembic upgrade head) берём DATABASE_URL.
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,  # ALTER TABLE в SQLite делается через пересоздание таблицы
        compare_type=True,
        **kwargs,
    )


def run_offline() -> None:
    _configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    # Внешние ключи выключены: пересоздание таблиц в SQLite не должно каскадно удалять данные.
    engine: Engine = make_engine(_database_url(), foreign_keys=False)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
