"""WebSocket 事件循环 — 连接管理 + 会话持久化 + 事件转发。

职责:
  1. 接收 iOS WS 消息 → 投递到 EventBus (prio=0)
  2. 消费 EventBus 输出 → 发送 WS 消息到 iOS
  3. 会话断开时缓存事件到 Redis，重连时回放
  4. 握手协议: 首条消息夹带 agentId/sessionId

握手流程:
  ① iOS → Server:  WS 连接 /ws/chat/{agent_id}/{session_id}
  ② iOS → Server:  message(chat) — 首条消息夹带握手信息
  ③ Server:        检查 session → 不存在则创建
  ④ Server → iOS:  phase(connected)
  ⑤ 握手完成，进入正常交互

协议: docs/development/websocket-protocol.md v7.0
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WSHandler:
    """WebSocket 连接处理器。

    每个 WS 连接对应一个 WSHandler 实例。
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 agent_id: str = "agent-001"):
        self._redis_url = redis_url
        self._agent_id = agent_id
        self._redis = None
        self._ws: Optional[WebSocket] = None
        self._session_id: str = "main"
        self._seq = 0
        self._connected = False

        # 消息队列（从 EventBus → WS 发送）
        self._outgoing: asyncio.Queue = asyncio.Queue()

    @property
    def redis(self):
        if self._redis is None:
            import redis as _redis
            self._redis = _redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def handle(self, ws: WebSocket, agent_id: str, session_id: str):
        """主入口 — 管理整个 WS 生命周期。

        Args:
            ws: FastAPI WebSocket 连接
            agent_id: Agent 实例 ID (URL 路径参数)
            session_id: 会话 ID (URL 路径参数)
        """
        self._ws = ws
        self._agent_id = agent_id
        self._session_id = session_id
        self._connected = False

        await ws.accept()
        logger.info(f"WS accepted: agent={agent_id} session={session_id}")

        try:
            # 启动收发协程
            recv_task = asyncio.create_task(self._recv_loop())
            send_task = asyncio.create_task(self._send_loop())

            # 等待任一完成
            done, pending = await asyncio.wait(
                [recv_task, send_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # 取消未完成的
            for task in pending:
                task.cancel()

        except WebSocketDisconnect:
            logger.info(f"WS disconnected: session={session_id}")
        except Exception as e:
            logger.error(f"WS error: {e}")
        finally:
            self._connected = False
            logger.info(f"WS closed: session={session_id}")

    async def _recv_loop(self):
        """接收 iOS 消息 → 投递到 Redis 命令通道。"""
        while True:
            try:
                raw = await self._ws.receive_text()
                envelope = json.loads(raw)

                msg_type = envelope.get("type", "")
                sub_type = envelope.get("subType", "")
                session_id = envelope.get("sessionId", self._session_id)
                seq = envelope.get("seq", 0)

                logger.debug(f"WS recv: type={msg_type}/{sub_type} seq={seq}")

                # 握手检测: 首条消息的 session 与 URL 不一致 → 自动创建
                if session_id and session_id != self._session_id:
                    logger.info(f"Session mismatch: url={self._session_id} msg={session_id}")
                    self._session_id = session_id

                # 心跳
                if msg_type == "heartbeat":
                    await self._send_envelope({
                        "role": "assistant",
                        "type": "heartbeat",
                        "subType": "pong",
                        "agentId": self._agent_id,
                        "sessionId": self._session_id,
                        "seq": seq,
                        "priority": 0,
                        "timestamp": _now(),
                        "payload": {},
                    })
                    continue

                # 推送消息到 Redis 命令通道 → Daemon 消费
                self.redis.lpush(
                    f"agent:cmd:{self._agent_id}",
                    json.dumps(envelope, ensure_ascii=False, default=str),
                )

            except WebSocketDisconnect:
                raise
            except json.JSONDecodeError:
                logger.warning(f"WS invalid JSON: {raw[:100]}")
            except Exception as e:
                logger.error(f"WS recv error: {e}")

    async def _send_loop(self):
        """从 outgoing 队列取消息 → 发送到 WS。"""
        while True:
            try:
                envelope = await asyncio.wait_for(self._outgoing.get(), timeout=30.0)
                if self._ws and self._connected:
                    raw = json.dumps(envelope, ensure_ascii=False)
                    await self._ws.send_text(raw)
            except asyncio.TimeoutError:
                continue  # 无消息，继续等待
            except WebSocketDisconnect:
                raise
            except Exception as e:
                logger.error(f"WS send error: {e}")

    # ── 外部接口 ─────────────────────────────────────

    async def send_envelope(self, envelope: dict):
        """外部调用 — 将信封加入发送队列。

        ws_handler 注入到 daemon 的 set_ws_send_fn 后，
        AgentRunLoop 通过此方法发送消息到 iOS。
        """
        self._seq += 1
        envelope["seq"] = envelope.get("seq", self._seq)
        envelope["agentId"] = envelope.get("agentId", self._agent_id)
        envelope["sessionId"] = envelope.get("sessionId", self._session_id)
        envelope["timestamp"] = envelope.get("timestamp", _now())
        await self._outgoing.put(envelope)

    async def send_and_persist(self, envelope: dict):
        """发送 + 持久化到 Redis（用于离线回放）。"""
        self._seq += 1
        envelope["seq"] = envelope.get("seq", self._seq)
        envelope["agentId"] = envelope.get("agentId", self._agent_id)
        envelope["sessionId"] = envelope.get("sessionId", self._session_id)
        envelope["timestamp"] = envelope.get("timestamp", _now())

        # 持久化到 Redis 列表
        try:
            key = f"agent:sessions:{self._agent_id}:{self._session_id}:events"
            self.redis.rpush(key, json.dumps(envelope, ensure_ascii=False, default=str))
            self.redis.expire(key, 86400)  # 24h TTL
        except Exception as e:
            logger.warning(f"Failed to persist event: {e}")

        await self._outgoing.put(envelope)

    async def _send_envelope(self, envelope: dict):
        """内部发送（不持久化）。"""
        await self._outgoing.put(envelope)

    async def replay_events(self, session_id: str) -> list[dict]:
        """回放已持久化的事件（重连时使用）。"""
        key = f"agent:sessions:{self._agent_id}:{session_id}:events"
        try:
            events_raw = self.redis.lrange(key, 0, -1)
            return [json.loads(e) for e in events_raw]
        except Exception:
            return []

    # ── 连接状态 ─────────────────────────────────────

    def mark_connected(self):
        """标记连接已建立（握手完成后调用）。"""
        self._connected = True
        logger.info(f"WS connected: session={self._session_id}")

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None
