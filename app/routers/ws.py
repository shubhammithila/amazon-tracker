import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.routers.auth import get_current_user
from app.scraper.engine import scrape_state

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        # Must not raise: broadcast() may already have evicted this socket, and
        # a bare list.remove() on a missing element throws ValueError inside the
        # disconnect handler.
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()


@router.websocket("/ws/progress")
async def progress_websocket(websocket: WebSocket):
    # Every REST route is gated by Depends(require_auth); this socket streams the
    # same scrape data (ASINs, titles, prices, sellers) and must be gated too.
    # WebSocket routes cannot use the HTTP redirect dependency, so check the
    # session cookie directly and refuse the handshake when it is missing.
    if not get_current_user(websocket):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket)
    last_payload: dict | None = None
    try:
        while True:
            state_data = scrape_state.to_dict()

            # Only ship the full results array while a scrape is actually
            # running — that is the only time the dashboard renders from it.
            # Sending ~250 result dicts every second to every client saturates
            # a t2.micro for no benefit once the run has finished.
            if state_data["running"]:
                state_data["results"] = scrape_state.results

            # Skip identical frames. Idle dashboards then cost nothing instead
            # of a JSON serialisation and send every second.
            if state_data != last_payload:
                await websocket.send_json(state_data)
                last_payload = dict(state_data)

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
