"""WebSocket-хаб в памяти: рассылка сообщений всем подключённым клиентам.

publish() можно вызывать из любого потока (синхронные эндпоинты FastAPI работают в пуле
потоков): сообщение раскладывается по очередям клиентов в цикле событий приложения.
У каждого клиента своя очередь, поэтому медленный клиент не тормозит остальных.
"""

import asyncio
import logging
from collections import deque
from typing import Any

from fastapi import WebSocket

from app.models.base import utcnow
from app.schemas.enums import WsMessageType
from app.schemas.ws import WsMessage

logger = logging.getLogger(__name__)

CLIENT_QUEUE_SIZE = 1000


class WsHub:
    def __init__(self, history_size: int = 500) -> None:
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Последние сообщения: для отладки и тестов (сколько event.created ушло и т.п.).
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, msg_type: WsMessageType, data: dict[str, Any]) -> dict[str, Any]:
        message = WsMessage(type=msg_type, data=data, ts=utcnow()).model_dump(mode="json")
        self.history.append(message)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._fanout, message)
        return message

    def _fanout(self, message: dict[str, Any]) -> None:
        for queue in list(self._clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("WS-клиент не успевает читать сообщения — отключаем")
                self._clients.discard(queue)

    async def serve(self, websocket: WebSocket) -> None:
        """Обслуживает одно подключение до его закрытия. Входящие сообщения игнорируются."""
        await websocket.accept()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
        self._clients.add(queue)
        try:
            await pump(websocket, queue)
        finally:
            self._clients.discard(queue)


async def pump(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Отправляет клиенту сообщения из очереди, пока он не отключится."""
    disconnected = asyncio.create_task(_wait_disconnect(websocket))
    try:
        while True:
            next_message = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {next_message, disconnected}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnected in done:
                next_message.cancel()
                break
            await websocket.send_json(next_message.result())
    except Exception:  # клиент пропал посреди отправки — это не ошибка сервера
        logger.debug("WS-клиент отключился при отправке", exc_info=True)
    finally:
        disconnected.cancel()


async def _wait_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
