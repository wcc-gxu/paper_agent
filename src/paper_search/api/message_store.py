"""MessageStore — WebSocket 消息持久化与重连回放。

回放规则（websocket-protocol.md §3.4）:
  - thinking / message(text) 流 → DISCARD（不回放）
  - tool(server) 历史 → 不回放，以 phase 当前进度替代
  - review（未过期 <30min）→ RE-SENT
  - error → ALL replayed
  - 已完成任务 → 最近 1 条 message(reply)，其余合并通知

使用方式:
    from paper_search.api.message_store import MessageStore
    store = MessageStore(db)
    await store.save_envelope(envelope)
    replay_msgs = await store.get_replay_messages("agent-001", "main")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 回放排除类型（priority=0 流式消息和展示消息）
_REPLAY_EXCLUDE_TYPES = {
    ("thinking", None),           # thinking 无子类型
    ("message", "text"),          # 流式文本
    ("tool", "server"),           # server 工具展示
    ("heartbeat", "ping"),
    ("heartbeat", "pong"),
    ("phase", None),              # phase 不作为历史回放（当前状态通过 connected 下发了）
}


class MessageStore:
    """WS 消息持久化层 — 存储 + 智能回放。"""

    def __init__(self, db):
        """初始化。

        Args:
            db: AgentDB 实例
        """
        self._db = db

    # ── 持久化 ────────────────────────────────────────────

    async def save_envelope(self, envelope: dict) -> int:
        """保存完整的协议信封到 ws_messages 表。

        Args:
            envelope: 符合 websocket-protocol.md 的完整消息信封

        Returns:
            数据库行 ID
        """
        role = envelope.get("role", "unknown")
        msg_type = envelope.get("type", "")
        subtype = envelope.get("subType", "") or ""
        payload = envelope.get("payload", {})
        seq = envelope.get("seq", 0)
        priority = envelope.get("priority", 0)

        return self._db.save_ws_message(
            agent_id=envelope.get("agentId", ""),
            session_id=envelope.get("sessionId", ""),
            seq=seq,
            role=role,
            type_=msg_type,
            subtype=subtype,
            payload=payload,
            priority=priority,
        )

    async def save_user_message(self, agent_id: str, session_id: str,
                                  seq: int, payload: dict) -> int:
        """快捷方法：保存用户消息。"""
        return self._db.save_ws_message(
            agent_id=agent_id, session_id=session_id, seq=seq,
            role="user", type_="message", subtype="chat",
            payload=payload, priority=1,
        )

    async def save_assistant_message(self, agent_id: str, session_id: str,
                                       msg_type: str, subtype: str,
                                       payload: dict, priority: int = 0,
                                       seq: int = 0) -> int:
        """快捷方法：保存助手消息。"""
        return self._db.save_ws_message(
            agent_id=agent_id, session_id=session_id, seq=seq,
            role="assistant", type_=msg_type, subtype=subtype,
            payload=payload, priority=priority,
        )

    # ── 回放 ──────────────────────────────────────────────

    async def get_replay_messages(self, agent_id: str, session_id: str,
                                    window_minutes: int = 30,
                                    limit: int = 200) -> list[dict]:
        """获取重连时应回放的消息，应用 §3.4 规则。

        回放规则:
        1. priority=0 流式/心跳/工具展示 → 不回放
        2. review(server→ios) 只回放 window_minutes 内未过期的
        3. error → 全部回放
        4. message(reply) → 回放最近 1 条

        Args:
            agent_id: Agent ID
            session_id: Session ID
            window_minutes: review 过期窗口（分钟）
            limit: 最大回放条数

        Returns:
            适合在重连后立即发送的消息列表
        """
        since_seq = 0  # 从第一条开始
        all_recent = self._db.get_ws_messages_for_replay(
            agent_id, session_id, since_seq=since_seq, limit=limit,
        )

        now = datetime.now(timezone.utc)
        replay = []
        has_reply = False

        for msg in reversed(all_recent):
            msg_type = msg.get("type", "")
            msg_sub = msg.get("subtype", "") or None
            created = msg.get("created_at", "")

            # Rule 1: skip excluded types
            if (msg_type, msg_sub) in _REPLAY_EXCLUDE_TYPES:
                continue
            if (msg_type, None) in _REPLAY_EXCLUDE_TYPES:
                continue

            # Rule 2: review expiration
            if msg_type == "review" and msg.get("role") == "assistant":
                if created:
                    try:
                        created_dt = datetime.fromisoformat(created)
                        age = (now - created_dt).total_seconds() / 60
                        if age > window_minutes:
                            continue  # expired
                    except (ValueError, TypeError):
                        pass

            # Rule 4: only keep most recent reply
            if msg_type == "message" and msg_sub == "reply":
                if has_reply:
                    continue
                has_reply = True

            # Parse payload from string if needed
            payload = msg.get("payload", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}

            replay.append({
                "role": msg.get("role", ""),
                "type": msg_type,
                "subType": msg_sub or "",
                "agentId": msg.get("agent_id", agent_id),
                "sessionId": msg.get("session_id", session_id),
                "seq": msg.get("seq", 0),
                "priority": msg.get("priority", 0),
                "timestamp": created,
                "payload": payload,
            })

        # Reverse to chronological order
        replay.reverse()
        return replay[:limit]

    async def get_last_reply(self, agent_id: str, session_id: str) -> Optional[dict]:
        """获取最近一条 message(reply)。

        Returns:
            格式化的消息信封，若无则返回 None
        """
        all_msgs = self._db.get_ws_messages_for_replay(
            agent_id, session_id, since_seq=0, limit=500,
        )
        for msg in reversed(all_msgs):
            if msg.get("type") == "message" and msg.get("subtype") == "reply":
                payload = msg.get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                return {
                    "role": "assistant",
                    "type": "message",
                    "subType": "reply",
                    "agentId": agent_id,
                    "sessionId": session_id,
                    "seq": 0,
                    "priority": 1,
                    "timestamp": msg.get("created_at", ""),
                    "payload": payload,
                }
        return None

    async def get_unexpired_reviews(self, agent_id: str, session_id: str,
                                      window_minutes: int = 30) -> list[dict]:
        """获取未过期的 pending review 消息。

        Args:
            window_minutes: 超过该时间的 review 视为过期

        Returns:
            未过期的 review 消息信封列表
        """
        all_msgs = self._db.get_ws_messages_for_replay(
            agent_id, session_id, since_seq=0, limit=500,
        )
        now = datetime.now(timezone.utc)
        reviews = []

        for msg in all_msgs:
            if msg.get("type") == "review" and msg.get("role") == "assistant":
                created = msg.get("created_at", "")
                if created:
                    try:
                        created_dt = datetime.fromisoformat(created)
                        age = (now - created_dt).total_seconds() / 60
                        if age > window_minutes:
                            continue
                    except (ValueError, TypeError):
                        pass

                payload = msg.get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}

                reviews.append({
                    "role": "assistant",
                    "type": "review",
                    "subType": msg.get("subtype", ""),
                    "agentId": agent_id,
                    "sessionId": session_id,
                    "seq": msg.get("seq", 0),
                    "priority": 2,
                    "timestamp": created,
                    "payload": payload,
                })

        return reviews
