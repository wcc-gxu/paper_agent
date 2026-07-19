"""消息窗口管理 — 档 2 滚动摘要 (hot path).

触发条件 (任一满足):
- messages 总数 >= SUMMARY_TRIGGER_COUNT (30)
- 累计 token >= SUMMARY_TRIGGER_TOKENS (16000)

做法:
1. 保留最新 SUMMARY_KEEP_RECENT (10) 条不动
2. 把更老的 K 条交给 LLM 总结成 1 条 SystemMessage
3. K > SUMMARY_BATCH_MAX (100) 时用 map-reduce 递归
4. 原 K 条 messages 归档到 sessions (summary JSONB) 表 (可回溯)

成本: 每次触发 1 次 LLM (map-reduce 时 N 次)
语义: 保语义 + 压长度, 仍 thread-scoped

参见: docs/development/memory-system.md §3.2
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from langchain_core.messages import BaseMessage, SystemMessage

from .message_trim import estimate_tokens

if TYPE_CHECKING:
    from .pgdb import PostgresAgentDB
    from .llm_client_v2 import LLMClientV2

logger = logging.getLogger(__name__)


SUMMARY_TRIGGER_COUNT: int = int(os.getenv("SUMMARY_TRIGGER_COUNT", "30"))
SUMMARY_TRIGGER_TOKENS: int = int(os.getenv("SUMMARY_TRIGGER_TOKENS", "16000"))
SUMMARY_KEEP_RECENT: int = int(os.getenv("SUMMARY_KEEP_RECENT", "10"))
SUMMARY_BATCH_MAX: int = int(os.getenv("SUMMARY_BATCH_MAX", "100"))
SUMMARY_PARTIAL_MAX_TOKENS: int = int(os.getenv("SUMMARY_PARTIAL_MAX_TOKENS", "6000"))

SUMMARY_PROMPT = """\
你是 Paper Agent 的对话摘要器。把下面的早期对话压缩成一段简短摘要,
要保留:
1) 用户表达的研究方向 / 关键词 / 偏好
2) 已完成的关键操作 (搜了什么 / 入库了什么 / 生成了什么综述)
3) 任何用户明确的好恶或选择

不要逐条复述。最多 300 字。用第三人称写,以便后续 LLM 把它当作背景信息。

早期对话:
{conversation}
"""


def _format_messages(messages: list[BaseMessage]) -> str:
    """把 BaseMessage 列表转成文本片段, 给摘要 LLM 输入用."""
    lines = []
    for m in messages:
        role = m.__class__.__name__.replace("Message", "")
        content = m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
        lines.append(f"[{role}] {content[:500]}")
    return "\n".join(lines)


class SummarizationNode:
    """档 2 滚动摘要节点 — hot path.

    用法 (在 MainAgent graph 入口或 evaluate_completion 收尾时):

        node = SummarizationNode(llm=..., db=...)
        if node.should_trigger(state.messages):
            new_messages = await node.summarize_and_archive(
                messages=state.messages,
                thread_id=session_id,
                agent_id=agent_id,
            )
            state.messages = new_messages
    """

    def __init__(self, llm: "LLMClientV2", db: "AgentDB"):
        self.llm = llm
        self.db = db

    # ── 触发判定 ──────────────────────────────────────────

    def should_trigger(self, messages: list[BaseMessage]) -> bool:
        if len(messages) >= SUMMARY_TRIGGER_COUNT:
            return True
        if estimate_tokens(messages) >= SUMMARY_TRIGGER_TOKENS:
            return True
        return False

    # ── 主流程 ────────────────────────────────────────────

    async def summarize_and_archive(
        self,
        messages: list[BaseMessage],
        thread_id: str,
        agent_id: str,
        reason: str = "",
    ) -> list[BaseMessage]:
        """压缩 + 归档.

        Returns:
            新消息列表: [summary SystemMessage] + 最新 SUMMARY_KEEP_RECENT 条
        """
        if len(messages) <= SUMMARY_KEEP_RECENT:
            return list(messages)

        recent = messages[-SUMMARY_KEEP_RECENT:]
        to_summarize = messages[:-SUMMARY_KEEP_RECENT]

        if len(to_summarize) > SUMMARY_BATCH_MAX:
            summary_text = await self._map_reduce_summarize(to_summarize)
        else:
            summary_text = await self._summarize_batch(to_summarize)

        # 归档
        summary_msg_id = f"summary-{uuid.uuid4().hex[:12]}"
        try:
            # 去重: 检查是否已有相同数量的归档
            existing = self._check_archive_exists(
                thread_id=thread_id,
                original_count=len(to_summarize),
            )
            if existing:
                logger.debug(
                    f"Skipping archive for {thread_id}: "
                    f"already archived (existing={existing})"
                )
            else:
                self._archive(
                    thread_id=thread_id,
                    agent_id=agent_id,
                    summary_msg_id=summary_msg_id,
                    original_messages=to_summarize,
                    reason=reason or f"trigger_count_{len(messages)}",
                )
        except Exception as e:
            logger.warning(f"sessions (summary JSONB) write/skip failed: {e}")

        # 拼回去
        summary_block = SystemMessage(
            content=f"[早期对话摘要 {summary_msg_id}]\n{summary_text}",
            id=summary_msg_id,
        )
        return [summary_block, *recent]

    # ── 内部 ──────────────────────────────────────────────

    async def _summarize_batch(self, messages: list[BaseMessage]) -> str:
        """单次摘要 (messages 数 <= SUMMARY_BATCH_MAX)."""
        prompt = SUMMARY_PROMPT.format(conversation=_format_messages(messages))
        try:
            resp = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                node="summary",
            )
            return resp.content.strip() if hasattr(resp, "content") else str(resp).strip()
        except Exception as e:
            logger.warning(f"summary LLM call failed: {e}")
            # Fail-soft: 返回粗略提示, 主流程不阻塞
            return f"(早期 {len(messages)} 条对话, 摘要失败)"

    async def _map_reduce_summarize(self, messages: list[BaseMessage]) -> str:
        """按 token 切分递归摘要 (messages 数 > SUMMARY_BATCH_MAX)."""
        # 简单分块: 按消息条数等分
        chunk_size = SUMMARY_BATCH_MAX // 2  # 每块 50 条
        chunks = [
            messages[i : i + chunk_size]
            for i in range(0, len(messages), chunk_size)
        ]
        logger.info(f"map-reduce summarize: {len(messages)} msgs → {len(chunks)} chunks")
        partials = []
        for c in chunks:
            partials.append(await self._summarize_batch(c))
        # reduce: 把所有 partial 摘要再总结
        reduce_input = "\n\n---\n\n".join(
            f"片段 {i+1}:\n{p}" for i, p in enumerate(partials)
        )
        prompt = (
            "把下面这些分段摘要合并成一份连贯的总摘要 (最多 400 字, 第三人称):\n\n"
            + reduce_input
        )
        try:
            resp = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                node="summary",
            )
            return resp.content.strip() if hasattr(resp, "content") else str(resp).strip()
        except Exception as e:
            logger.warning(f"map-reduce reduce failed: {e}")
            # Fail-soft: 拼接 partial
            return "\n\n".join(partials)

    def _check_archive_exists(
        self, thread_id: str, original_count: int
    ) -> Optional[str]:
        """检查是否已有相同数量的归档记录（去重）."""
        try:
            row = self.db.conn.execute(
                """SELECT id FROM sessions (summary JSONB)
                   WHERE session_id = %s AND original_count = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (thread_id, original_count),
            ).fetchone()
            return row["id"] if row else None
        except Exception:
            return None

    def _archive(
        self,
        thread_id: str,
        agent_id: str,
        summary_msg_id: str,
        original_messages: list[BaseMessage],
        reason: str,
    ) -> None:
        """把原始 messages 写入 sessions (summary JSONB) 表 (PostgreSQL schema).

        兼容 init_db.sql DDL: id, session_id, user_id, summary_text, summary_type,
        start_msg_id, end_msg_id, metadata (JSONB), thread_id, agent_id, 等.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        original_json = json.dumps(
            [_message_to_dict(m) for m in original_messages],
            ensure_ascii=False,
        )
        token_count = estimate_tokens(original_messages)
        archive_id = f"arch-{summary_msg_id}"

        # 元数据打包进 JSONB (含旧 schema 的扩展字段)
        metadata = json.dumps({
            "summary_msg_id": summary_msg_id,
            "original_messages_json": original_json,
            "agent_id": agent_id,
            "reason": reason,
        }, ensure_ascii=False)

        self.db.conn.execute(
            """INSERT INTO sessions (summary JSONB)
               (id, session_id, user_id, summary_text, summary_type,
                start_msg_id, end_msg_id, metadata,
                thread_id, agent_id, archived_at, summary_msg_id,
                original_messages_json, original_count, token_count, reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                       %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                archive_id,
                thread_id,                      # session_id
                "user-default",                 # user_id
                "(see metadata)",               # summary_text
                "rolling",                      # summary_type
                None,                           # start_msg_id
                summary_msg_id,                 # end_msg_id
                metadata,                       # metadata (JSONB)
                thread_id,                      # thread_id
                agent_id,                       # agent_id
                now,                            # archived_at
                summary_msg_id,                 # summary_msg_id
                original_json,                  # original_messages_json
                len(original_messages),         # original_count
                token_count,                    # token_count
                reason,                         # reason
            ),
        )
        self.db.conn.commit()


def _message_to_dict(m: BaseMessage) -> dict:
    """简化 message 序列化, 仅保留 role/content/id."""
    return {
        "role": m.__class__.__name__,
        "content": m.content if isinstance(m.content, str)
                   else json.dumps(m.content, ensure_ascii=False),
        "id": getattr(m, "id", None),
    }
