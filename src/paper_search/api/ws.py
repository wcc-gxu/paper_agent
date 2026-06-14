"""WebSocket 连接管理器."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class WebSocketManager:
    """管理活跃的 WebSocket 连接."""

    def __init__(self):
        self._connections: dict[str, list[Any]] = {}  # session_id → [ws, ...]

    async def connect(self, session_id: str, websocket):
        if session_id not in self._connections:
            self._connections[session_id] = []
        self._connections[session_id].append(websocket)
        logger.debug(f"WS connected: session={session_id}, total={len(self._connections[session_id])}")

    async def disconnect(self, session_id: str, websocket):
        if session_id in self._connections:
            self._connections[session_id] = [
                ws for ws in self._connections[session_id] if ws != websocket
            ]
            if not self._connections[session_id]:
                del self._connections[session_id]
        logger.debug(f"WS disconnected: session={session_id}")

    async def broadcast(self, session_id: str, message: dict):
        """向指定会话的所有连接广播消息."""
        if session_id in self._connections:
            import json
            text = json.dumps(message, ensure_ascii=False, default=str)
            for ws in self._connections[session_id]:
                try:
                    await ws.send_text(text)
                except Exception as e:
                    logger.warning(f"WS send failed: {e}")

    @property
    def active_sessions(self) -> list[str]:
        return list(self._connections.keys())


ws_manager = WebSocketManager()
