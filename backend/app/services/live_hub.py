"""Живой просмотр: последний кадр каждого источника и рассылка его подписчикам.

В отличие от журнала событий здесь важна свежесть, а не полнота: у каждого клиента очередь
на 2 сообщения, и при переполнении выбрасывается самое старое.
"""

import asyncio
import base64
from typing import Any

from fastapi import WebSocket

from app.models.base import utcnow
from app.services.notifier import pump

CLIENT_QUEUE_SIZE = 2


class LiveHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._latest_jpeg: dict[str, bytes] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscriber_count(self, source_id: str) -> int:
        return len(self._subscribers.get(source_id, ()))

    def latest_jpeg(self, source_id: str) -> bytes | None:
        return self._latest_jpeg.get(source_id)

    def publish(self, source_id: str, meta: dict[str, Any], jpeg: bytes | None) -> None:
        image = None
        if jpeg is not None:
            self._latest_jpeg[source_id] = jpeg
            image = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        message = {
            "type": "live.frame",
            "data": {**meta, "source_id": source_id, "image": image},
            "ts": utcnow().isoformat().replace("+00:00", "Z"),
        }
        self._latest[source_id] = message
        self._dispatch(source_id, message)

    def end(self, source_id: str) -> None:
        """Анализ источника закончился: подписчики узнают об этом сразу."""
        self._latest.pop(source_id, None)
        self._latest_jpeg.pop(source_id, None)
        message = {
            "type": "live.ended",
            "data": {"source_id": source_id},
            "ts": utcnow().isoformat().replace("+00:00", "Z"),
        }
        self._dispatch(source_id, message)

    def _dispatch(self, source_id: str, message: dict[str, Any]) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._fanout, source_id, message)

    def _fanout(self, source_id: str, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(source_id, ())):
            if queue.full():
                queue.get_nowait()  # выбрасываем устаревший кадр
            queue.put_nowait(message)

    async def serve(self, websocket: WebSocket, source_id: str) -> None:
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        if source_id in self._latest:
            queue.put_nowait(self._latest[source_id])  # сразу показываем последний кадр
        subscribers = self._subscribers.setdefault(source_id, set())
        subscribers.add(queue)
        try:
            await pump(websocket, queue)
        finally:
            subscribers.discard(queue)
