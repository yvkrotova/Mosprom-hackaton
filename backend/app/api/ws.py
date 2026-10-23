"""WebSocket: тревоги и статусы (/ws/events) и живой просмотр источника (/ws/live/{id})."""

from fastapi import APIRouter, WebSocket

router = APIRouter()


@router.websocket("/ws/events")
async def events_ws(websocket: WebSocket) -> None:
    await websocket.app.state.hub.serve(websocket)


@router.websocket("/ws/live/{source_id}")
async def live_ws(websocket: WebSocket, source_id: str) -> None:
    await websocket.app.state.live_hub.serve(websocket, source_id)
