"""LangGraph Checkpointer using PostgreSQL.

Replaces AsyncSqliteSaver (checkpointer.py) with native PostgreSQL checkpointer.
Requires: langgraph-checkpoint-postgres>=2.0

Usage:
    from .pg_checkpointer import build_checkpointer, close_checkpointer
    saver = await build_checkpointer(dsn)
    graph.compile(checkpointer=saver)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def build_checkpointer(dsn: str = "") -> Any:
    """Build LangGraph AsyncPostgresSaver from DATABASE_URL.

    Args:
        dsn: PostgreSQL connection string (default: DATABASE_URL env var).

    Returns:
        AsyncPostgresSaver instance ready for graph.compile(checkpointer=...).
    """
    import os
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    dsn = dsn or os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL 未设置，无法创建 PostgreSQL Checkpointer。"
        )

    saver = AsyncPostgresSaver.from_conn_string(dsn)
    await saver.setup()
    logger.info("PostgreSQL Checkpointer 已初始化")
    return saver


async def close_checkpointer(saver: Any) -> None:
    """Close the checkpointer connection."""
    if saver is None:
        return
    try:
        if hasattr(saver, "aclose"):
            await saver.aclose()
        elif hasattr(saver, "close"):
            saver.close()
    except Exception as e:
        logger.warning(f"关闭 Checkpointer 失败: {e}")
