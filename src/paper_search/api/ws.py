"""WebSocket 连接管理器.

双层索引: agent_id → session_id → [ws, ...]
与 docs/development/websocket-protocol.md 协议一致。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class WebSocketManager:
    """管理活跃的 WebSocket 连接 — 双层 (agent_id + session_id) 索引."""

    def __init__(self):
        # agent_id → { session_id → [websocket, ...] }
        self._connections: dict[str, dict[str, list[Any]]] = {}

    async def connect(self, agent_id: str, session_id: str, websocket):
        """注册一个 WebSocket 连接到双层索引."""
        if agent_id not in self._connections:
            self._connections[agent_id] = {}
        if session_id not in self._connections[agent_id]:
            self._connections[agent_id][session_id] = []
        self._connections[agent_id][session_id].append(websocket)
        logger.debug(
            "WS connected: agent=%s, session=%s, total_sessions=%d, conns_in_session=%d",
            agent_id, session_id,
            len(self._connections[agent_id]),
            len(self._connections[agent_id][session_id]),
        )

    async def disconnect(self, agent_id: str, session_id: str, websocket):
        """从双层索引中移除 WebSocket 连接."""
        if agent_id in self._connections and session_id in self._connections[agent_id]:
            self._connections[agent_id][session_id] = [
                ws for ws in self._connections[agent_id][session_id] if ws != websocket
            ]
            if not self._connections[agent_id][session_id]:
                del self._connections[agent_id][session_id]
            if not self._connections[agent_id]:
                del self._connections[agent_id]
        logger.debug("WS disconnected: agent=%s, session=%s", agent_id, session_id)

    async def broadcast(self, agent_id: str, session_id: str, message: dict):
        """向指定 agent+session 的所有连接广播消息."""
        import json

        text = json.dumps(message, ensure_ascii=False, default=str)
        try:
            sessions = self._connections.get(agent_id, {})
            conns = sessions.get(session_id, [])
        except (TypeError, AttributeError):
            return
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.warning("WS send failed: agent=%s, session=%s, error=%s", agent_id, session_id, e)

    async def broadcast_to_agent(self, agent_id: str, message: dict):
        """向指定 agent 所有 session 广播消息."""
        import json

        text = json.dumps(message, ensure_ascii=False, default=str)
        sessions = self._connections.get(agent_id, {})
        for sid, conns in sessions.items():
            for ws in conns:
                try:
                    await ws.send_text(text)
                except Exception as e:
                    logger.warning("WS send failed: agent=%s, session=%s, error=%s", agent_id, sid, e)

    @property
    def active_agents(self) -> list[str]:
        return list(self._connections.keys())

    def active_sessions(self, agent_id: str | None = None) -> list[str]:
        if agent_id:
            return list(self._connections.get(agent_id, {}).keys())
        all_sessions: list[str] = []
        for sessions in self._connections.values():
            all_sessions.extend(sessions.keys())
        return all_sessions

    @property
    def total_connections(self) -> int:
        return sum(
            len(conns)
            for sessions in self._connections.values()
            for conns in sessions.values()
        )


ws_manager = WebSocketManager()
