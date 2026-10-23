"""Общие зависимости FastAPI."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.services.live_hub import LiveHub
from app.services.notifier import WsHub
from app.services.worker_manager import WorkerManager


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _hub(request: Request) -> WsHub:
    return request.app.state.hub


def _workers(request: Request) -> WorkerManager:
    return request.app.state.worker_manager


def _live(request: Request) -> LiveHub:
    return request.app.state.live_hub


DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(_settings)]
HubDep = Annotated[WsHub, Depends(_hub)]
WorkersDep = Annotated[WorkerManager, Depends(_workers)]
LiveDep = Annotated[LiveHub, Depends(_live)]
