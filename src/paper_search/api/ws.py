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
        # 提取客户端 IP（兼容不同 proxy 层级）
        client_ip = ""
        try:
            client = getattr(websocket, "client", None)
            if client:
                client_ip = f"{client.host}:{client.port}"
            else:
                client_ip = "unknown"
        except Exception:
            client_ip = "unknown"
        logger.info(
            "📱 WS CONNECT | agent=%s session=%s client=%s total_conns=%d",
            agent_id, session_id, client_ip, self.total_connections,
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
        logger.info(
            "🔌 WS DISCONNECT | agent=%s session=%s total_conns=%d",
            agent_id, session_id, self.total_connections,
        )

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

    def get_online_sessions(self, agent_id: str) -> list[str]:
        """Phase 1: 列出某 agent 当前在线的 session_id（outbox_poller 用）。"""
        return list(self._connections.get(agent_id, {}).keys())

    def get_websocket(self, agent_id: str, session_id: str):
        """Phase 1: 获取某 agent+session 的首个 ws 连接（outbox_poller 用）。

        多个连接到同一 session 的场景很少见；返回首个即可。
        如需广播到该 session 所有连接，请用 broadcast()。
        """
        conns = self._connections.get(agent_id, {}).get(session_id, [])
        return conns[0] if conns else None

    def is_agent_online(self, agent_id: str) -> bool:
        """Phase 1: 判断 agent 是否有任意活跃连接。"""
        sessions = self._connections.get(agent_id, {})
        return any(conns for conns in sessions.values())

    async def send_and_persist(self, agent_id: str, session_id: str,
                                envelope: dict, store=None) -> int:
        """发送 WS 消息并持久化到 MessageStore。

        Args:
            agent_id: Agent ID
            session_id: Session ID
            envelope: 协议信封
            store: MessageStore 实例（若为 None 则只发送不持久化）

        Returns:
            持久化消息的 DB 行 ID，或 0
        """
        msg_id = 0
        if store is not None:
            try:
                msg_id = await store.save_envelope(envelope)
            except Exception as e:
                logger.warning(f"Failed to persist message: {e}")

        import json
        text = json.dumps(envelope, ensure_ascii=False, default=str)
        sessions = self._connections.get(agent_id, {})
        conns = sessions.get(session_id, [])
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.warning(f"WS send failed: agent={agent_id}, session={session_id}, error={e}")

        return msg_id


ws_manager = WebSocketManager()


# ═══════════════════════════════════════════════════════════════
# Subscription Notification Bridge
# ═══════════════════════════════════════════════════════════════
#
# Celery Worker (subscription_check_task) 发布新论文通知到
# Redis Pub/Sub channel "agent:notifications"。
# API 进程后台订阅该 channel，收到通知后通过 ws_manager
# 推送给所有连接的客户端。


async def start_notification_listener(
    redis_url: str = "redis://localhost:6379/0",
    agent_id: str = "agent-001",
):
    """后台任务: 监听 Redis Pub/Sub 订阅通知并推送到 WebSocket。

    应在 FastAPI startup 事件中调用:
        asyncio.create_task(start_notification_listener())

    Args:
        redis_url: Redis 连接 URL
        agent_id: 默认 Agent ID (用于 WebSocket 广播)
    """
    try:
        import redis.asyncio as aioredis
    except ImportError:
        logger.warning(
            "redis.asyncio not available — subscription notifications disabled"
        )
        return

    channel = "agent:notifications"

    try:
        r = aioredis.from_url(redis_url, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)
        logger.info(f"Notification listener started on channel: {channel}")
    except Exception as e:
        logger.warning(f"Failed to connect notification listener: {e}")
        return

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                import json
                data = json.loads(message["data"])
            except Exception:
                continue

            # Forward to connected WebSocket clients
            subscription_id = data.get("subscription_id", "")
            subscription_name = data.get("subscription_name", "")
            new_papers = data.get("new_papers", [])

            from datetime import datetime, timezone
            import uuid

            # v10: 订阅推送统一走 message/reply（priority=high → APNs 触发）
            total = len(new_papers)
            head_lines = []
            for i, p in enumerate(new_papers[:5], 1):
                title = (p.get("title") or "").strip() or "(无标题)"
                head_lines.append(f"{i}. {title}")
            preview_text = "\n".join(head_lines)
            content = (
                f"📡 订阅《{subscription_name or subscription_id}》"
                f"今日新增 {total} 篇:\n\n{preview_text}"
            )

            envelope = {
                "type": "message",
                "subType": "reply",
                "msg_id": str(uuid.uuid4()),
                "agentId": agent_id,
                "sessionId": "main",
                "priority": "high",
                "priorityKind": "high",
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                # 过渡期保留 role（v10 删除，可选向后兼容）
                "role": "assistant",
                "payload": {
                    "content": content,
                    # 结构化数据放 payload，便于 iOS 升级后展开
                    "subscription_id": subscription_id,
                    "subscription_name": subscription_name,
                    "new_papers": new_papers,
                    "total_new": total,
                },
            }

            # Broadcast to all active sessions for the default agent
            await ws_manager.broadcast_to_agent(agent_id, envelope)

    except Exception as e:
        logger.warning(f"Notification listener disconnected: {e}")
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await r.close()
        except Exception:
            pass
