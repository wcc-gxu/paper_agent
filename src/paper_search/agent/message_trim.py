"""消息窗口管理 — 档 1 trim_messages 薄包装.

每次 MainAgent 入口在构建 prompt 前调 `trim_for_context()`, 按 token 数滚动
保留最新 K 条; 老消息要么进入档 2 摘要, 要么丢弃.

参数 (与 docs/development/memory-system.md §3.1 对齐):
- MSG_TRIM_MAX_TOKENS: 8000
- MSG_TRIM_KEEP_LAST: 10
- approximate token counter (langchain_core 内置, 不依赖具体 LLM 客户端)
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from langchain_core.messages import BaseMessage, trim_messages

logger = logging.getLogger(__name__)


MSG_TRIM_MAX_TOKENS: int = int(os.getenv("MSG_TRIM_MAX_TOKENS", "8000"))
MSG_TRIM_KEEP_LAST: int = int(os.getenv("MSG_TRIM_KEEP_LAST", "10"))


def trim_for_context(
    messages: Iterable[BaseMessage] | list[BaseMessage],
    max_tokens: int = MSG_TRIM_MAX_TOKENS,
    keep_last: int = MSG_TRIM_KEEP_LAST,
    include_system: bool = True,
) -> list[BaseMessage]:
    """按 token 阈值裁剪消息列表, 保留最新 keep_last 条不动.

    Args:
        messages: 完整消息列表 (含 SystemMessage / HumanMessage / AIMessage / ToolMessage)
        max_tokens: token 总上限; 超过则从头开始砍
        keep_last: 强制保留最新 N 条（不参与裁剪）
        include_system: 是否在 token 预算里也保留 system 消息

    Returns:
        裁剪后的消息列表; 顺序保持原序

    Note:
        - 内部用 langchain_core 的 approximate token counter (每 token ≈ 4 chars)
        - allow_partial=False 保证 ToolMessage 与配对的 ToolCall 不被拆开
    """
    msg_list = list(messages) if not isinstance(messages, list) else messages
    if not msg_list:
        return []

    # 当 keep_last >= 总条数时直接返回
    if len(msg_list) <= keep_last:
        return msg_list

    # langchain_core 的 trim_messages: strategy='last' 从末尾保留;
    # max_tokens 是预算上限.
    try:
        trimmed = trim_messages(
            msg_list,
            max_tokens=max_tokens,
            token_counter="approximate",
            strategy="last",
            allow_partial=False,
            include_system=include_system,
        )
    except Exception as e:
        logger.warning(f"trim_messages failed, fallback to keep_last only: {e}")
        # Fail-soft: 至少保证返回最新 keep_last 条
        return msg_list[-keep_last:]

    # 兜底: 如果裁剪后比 keep_last 还少, 强制保留最新 keep_last 条
    if len(trimmed) < keep_last:
        return msg_list[-keep_last:]
    return trimmed


def estimate_tokens(messages: Iterable[BaseMessage]) -> int:
    """估算 token 数 (approximate, 与 trim_messages 一致)."""
    total_chars = sum(
        len(m.content) if isinstance(m.content, str)
        else sum(len(str(p)) for p in m.content)
        for m in messages
    )
    # 约 4 chars / token (英文); 中文按 1.5 chars / token 保守估
    return total_chars // 3
