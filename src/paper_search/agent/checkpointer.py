"""LangGraph Checkpointer 适配 — 短期记忆 (thread-scoped) 持久化.

设计:
- 与 AgentDB 同库 (~/.paper_search/agent.db)，独立 aiosqlite 连接
- 由 `graph.compile(checkpointer=...)` 自动注入
- 用 `config={"configurable": {"thread_id": session_id}}` 索引
- thread_id 即 WebSocket session_id

参见: docs/development/memory-system.md §2.1
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .db import AgentDB

logger = logging.getLogger(__name__)


async def build_checkpointer(
    db: Optional[AgentDB] = None,
    db_path: Optional[Path] = None,
) -> AsyncSqliteSaver:
    """构建 LangGraph Checkpointer (AsyncSqliteSaver)，与 AgentDB 同库.

    Args:
        db: 现有 AgentDB 实例 (优先用其 db_path)
        db_path: 直接指定 SQLite 文件路径; 与 db 二选一

    Returns:
        已 setup 完成的 AsyncSqliteSaver, 可直接传给 graph.compile(checkpointer=...)

    Note:
        - 内部创建独立 aiosqlite.Connection (与 AgentDB 的 sync sqlite3.Connection 互不干扰)
        - 都走 WAL 模式 (AgentDB 已开启), 多连接并发读写安全
        - 调用方需在进程退出时调 saver.conn.close() (或包到 lifespan 里)
    """
    if db is None and db_path is None:
        raise ValueError("build_checkpointer 需要 db 或 db_path 之一")
    target_path = str(db.db_path) if db is not None else str(db_path)
    # aiosqlite.connect 是 awaitable, 不需要 async with — 我们要长期持有这个 conn
    conn = await aiosqlite.connect(target_path)
    # WAL 模式（AgentDB 已设置, 这里幂等再确认）
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=30000")
    await conn.commit()

    saver = AsyncSqliteSaver(conn=conn)
    # setup() 创建 langgraph 标准 3 表: checkpoints / checkpoint_blobs / checkpoint_writes
    await saver.setup()
    logger.info(f"Checkpointer ready (db={target_path})")
    return saver


async def close_checkpointer(saver: AsyncSqliteSaver) -> None:
    """优雅关闭 — 在 daemon 退出时调用."""
    try:
        await saver.conn.close()
        logger.info("Checkpointer connection closed")
    except Exception as e:
        logger.warning(f"close_checkpointer failed: {e}")
