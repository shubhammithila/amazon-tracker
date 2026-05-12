import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.scraper.engine import scrape_state

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.active_connections.remove(conn)


manager = ConnectionManager()


@router.websocket("/ws/progress")
async def progress_websocket(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            state_data = scrape_state.to_dict()
            state_data["results"] = scrape_state.results
            await websocket.send_json(state_data)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
