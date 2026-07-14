"""LangGraph Store using PostgreSQL.

Replaces AsyncSqliteStore + DualBackendStore (store.py) with native PostgreSQL store.
Requires: langgraph-checkpoint-postgres>=2.0

Usage:
    from .pg_store import build_store, close_store
    store = await build_store(dsn)
    graph.compile(store=store)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def build_store(dsn: str = "") -> Any:
    """Build LangGraph AsyncPostgresStore from DATABASE_URL.

    Args:
        dsn: PostgreSQL connection string (default: DATABASE_URL env var).

    Returns:
        AsyncPostgresStore instance ready for graph.compile(store=...).
    """
    import os
    from langgraph.store.postgres.aio import AsyncPostgresStore

    dsn = dsn or os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL 未设置，无法创建 PostgreSQL Store。"
        )

    store = AsyncPostgresStore.from_conn_string(dsn)
    await store.setup()
    logger.info("PostgreSQL Store 已初始化")
    return store


async def close_store(store: Any) -> None:
    """Close the store connection."""
    if store is None:
        return
    try:
        if hasattr(store, "aclose"):
            await store.aclose()
        elif hasattr(store, "close"):
            store.close()
    except Exception as e:
        logger.warning(f"关闭 Store 失败: {e}")
