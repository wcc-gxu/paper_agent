"""MessageStore — WebSocket 消息持久化层 (Phase 1 重写)。

旧版基于 priority(int)/seq 字段，Phase 1 改为基于 msg_id (UUID)/priority_kind/
delivered_sessions 的新 schema。所有出站消息通过 outbox_publish 双写到
ws_messages 表 + Redis outbox List；本模块提供查询 / 回放接口。

主要 API:
  - get_recent_messages(): 获取最近 N 条历史（构造上下文用）
  - get_undelivered_for_session(): iOS 重连同步用
  - get_unexpired_reviews(): plan / clarify 类待处理消息
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class MessageStore:
    """WS 消息存储与查询层 — 适配 Phase 1 outbox schema。"""

    def __init__(self, db):
        self._db = db

    # ── 写入（旧 API 兼容） ───────────────────────────

    async def save_envelope(self, envelope: dict, correlation_id: str = "") -> str:
        """保存 envelope 到 ws_messages（Phase 1 schema，返回 msg_id）。"""
        return self._db.save_outbox_envelope(envelope, correlation_id=correlation_id)

    # ── 查询 ──────────────────────────────────────────

    async def get_recent_messages(self, agent_id: str, session_id: str,
                                   limit: int = 50,
                                   include_silent: bool = False) -> list[dict]:
        """获取最近 N 条历史消息（用于 LLM 上下文构造）。

        默认排除 silent (流式 thinking)。
        """
        rows = self._db.conn.execute(
            f"""SELECT * FROM ws_messages
               WHERE agent_id=? AND session_id=?
                 {"" if include_silent else "AND priority_kind != 'silent'"}
               ORDER BY id DESC LIMIT ?""",
            (agent_id, session_id, limit),
        ).fetchall()
        out = []
        for r in reversed(rows):  # chronological order
            try:
                payload = json.loads(r["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            out.append({
                "msg_id": r["msg_id"] or "",
                "type": r["type"],
                "subType": r["subtype"],
                "role": r["role"],
                "payload": payload,
                "timestamp": r["created_at"],
                "priority": r["priority_kind"] or "normal",
                "priorityKind": r["priority_kind"] or "normal",
            })
        return out

    async def get_undelivered_for_session(self, agent_id: str, session_id: str,
                                           since_msg_id: str = "",
                                           hours: int = 24,
                                           limit: int = 500) -> list[dict]:
        """获取 session 未送达的消息（sync_request 用，回放历史）。"""
        return self._db.get_undelivered_messages(
            agent_id, session_id,
            since_msg_id=since_msg_id, hours=hours, limit=limit,
        )

    async def get_unexpired_reviews(self, agent_id: str, session_id: str,
                                     window_minutes: int = 30) -> list[dict]:
        """获取未过期的待用户响应消息（ask_user_question / propose_plan）。

        用于 iOS 重连时显式重发"还在等你回答"的提问。
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
        rows = self._db.conn.execute(
            """SELECT * FROM ws_messages
               WHERE agent_id=? AND session_id=?
                 AND ((type='tool' AND subtype IN ('ask_user_question', 'propose_plan'))
                      OR (type='ask'))
                 AND role='assistant'
                 AND created_at >= ?
               ORDER BY id ASC""",
            (agent_id, session_id, cutoff),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except (ValueError, TypeError):
                payload = {}
            out.append({
                "msg_id": r["msg_id"] or "",
                "type": r["type"],
                "subType": r["subtype"],
                "role": r["role"],
                "agentId": r["agent_id"],
                "sessionId": r["session_id"],
                "timestamp": r["created_at"],
                "priority": r["priority_kind"] or "high",
                "priorityKind": r["priority_kind"] or "high",
                "payload": payload,
            })
        return out

    async def mark_delivered(self, msg_id: str, session_id: str):
        """标记某 msg 已送达某 session。"""
        self._db.mark_message_delivered(msg_id, session_id)
