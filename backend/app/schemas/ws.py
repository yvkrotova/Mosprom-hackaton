"""Сообщения WebSocket /ws/events."""

from typing import Any

from pydantic import BaseModel

from app.schemas.common import UtcDatetime
from app.schemas.enums import SourceStatus, WsMessageType


class WsMessage(BaseModel):
    type: WsMessageType
    data: dict[str, Any]  # EventOut для event.*, SourceStatusData для source.status
    ts: UtcDatetime


class SourceStatusData(BaseModel):
    id: str
    name: str
    status: SourceStatus
    prev_status: SourceStatus | None
    last_error: str | None
    last_heartbeat_at: UtcDatetime | None
