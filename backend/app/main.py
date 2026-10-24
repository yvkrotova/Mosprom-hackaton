"""Создание FastAPI-приложения.

Запуск: uvicorn app.main:app --reload
"""

import asyncio
import logging
import mimetypes
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import internal, ws
from app.api.v1 import api_router
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory, run_migrations
from app.errors import install_error_handlers
from app.schemas.common import ErrorResponse
from app.services import event_service, source_service
from app.services.live_hub import LiveHub
from app.services.notifier import WsHub
from app.services.worker_manager import WorkerManager

logger = logging.getLogger(__name__)

# Windows часто не знает шрифт woff2 и видео mp4 — без типа браузер может их не принять.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("video/mp4", ".mp4")


async def _heartbeat_watchdog(app: FastAPI) -> None:
    """Раз в несколько секунд: running без heartbeat дольше HEARTBEAT_TIMEOUT_S -> disconnected."""
    settings: Settings = app.state.settings
    period = max(0.5, min(5.0, settings.heartbeat_timeout_s / 3))

    def check() -> None:
        with app.state.session_factory() as session:
            source_service.check_heartbeats(session, app.state.hub, settings.heartbeat_timeout_s)

    while True:
        await asyncio.sleep(period)
        try:
            await asyncio.to_thread(check)
        except Exception:
            logger.exception("Ошибка проверки heartbeat")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.internal_token:
        # Воркеры, запущенные этим backend, получат токен через окружение.
        settings.internal_token = secrets.token_urlsafe(32)
        logger.warning("INTERNAL_TOKEN не задан — сгенерирован случайный на время работы backend")

    for directory in (settings.storage_dir, settings.videos_dir, settings.evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.auto_migrate:
            run_migrations(settings.database_url)
        app.state.hub.bind_loop(asyncio.get_running_loop())
        app.state.live_hub.bind_loop(asyncio.get_running_loop())
        app.state.worker_manager.recover()
        watchdog = asyncio.create_task(_heartbeat_watchdog(app))
        yield
        watchdog.cancel()
        await asyncio.to_thread(app.state.worker_manager.shutdown)
        app.state.engine.dispose()

    app = FastAPI(
        title="LinQ Safety API",
        version="0.1.0",
        description="Видеоконтроль промышленной безопасности. Контракт: docs/CONTRACT.md",
        lifespan=lifespan,
        responses={
            404: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
    )
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.hub = WsHub()
    app.state.live_hub = LiveHub()
    app.state.worker_manager = WorkerManager(
        settings, app.state.session_factory, app.state.hub, app.state.live_hub
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(api_router)
    app.include_router(internal.router)
    app.include_router(ws.router)
    app.mount("/media", StaticFiles(directory=settings.storage_dir), name="media")
    event_service.configure_media(settings.storage_dir)
    # Фронтенд на корне сайта — последним, чтобы не перекрывать API.
    frontend = settings.frontend_dir
    if frontend and (frontend / "index.html").is_file():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    else:
        logger.info("Фронтенд не найден (%s) — отдаётся только API", frontend)
    return app


app = create_app()
