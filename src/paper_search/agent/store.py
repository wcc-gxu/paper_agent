"""LangGraph Store 适配 — 长期记忆 (cross-thread) 持久化.

设计:
- 主体走 AsyncSqliteStore（与 AgentDB 同库，独立 aiosqlite 连接）
- DualBackendStore 提供命名空间路由能力，供未来按 namespace 切换后端
- 当前 Phase 2B: 所有 namespace 落 SQLite；ChromaDB 仍由 chroma_store.ChromaStoreV2
  独立承担 RAG 向量检索（与 Store 解耦）
- Phase 2D 可选优化: knowledge namespace 接 ChromaBackedStore

namespace 三层 8 个 kind:
    (agent_id, "preferences")
    (agent_id, "profile")
    (agent_id, "episodes", session_id)
    (agent_id, "topics", topic_slug)
    (agent_id, "strategies")
    (agent_id, "errors")
    (agent_id, "knowledge", "papers")
    (agent_id, "knowledge", "chunks")

参见: docs/development/memory-system.md §2.2
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import aiosqlite
from langgraph.store.base import BaseStore, Op, Result
from langgraph.store.sqlite.aio import AsyncSqliteStore

from .db import AgentDB

logger = logging.getLogger(__name__)


# namespace 第二层 kind → 后端名（'sqlite' / 'chromadb'）.
# Phase 2B 实现: 所有 namespace 走 SQLite 单一后端;
# Phase 2D 评估是否给 episodes/topics/knowledge 接 ChromaBackedStore.
NAMESPACE_BACKEND_ROUTES: dict[str, str] = {
    "preferences": "sqlite",
    "profile":     "sqlite",
    "strategies":  "sqlite",
    "errors":      "sqlite",
    "episodes":    "sqlite",   # Phase 2D 评估改 chromadb
    "topics":      "sqlite",   # Phase 2D 评估改 chromadb
    "knowledge":   "sqlite",   # RAG 仍走 ChromaStoreV2 独立路径; 这里仅元数据
}

# 已知 namespace kind 集合（用于校验/审计）
KNOWN_KINDS = frozenset(NAMESPACE_BACKEND_ROUTES.keys())


class DualBackendStore(BaseStore):
    """按 namespace 路由的双后端 Store.

    当前: 仅 SQLite 一个真实后端 (内部组合 AsyncSqliteStore);
    保留 dual 抽象供 Phase 2D 接入 ChromaBackedStore 时无需改调用方.
    """

    def __init__(
        self,
        sqlite_store: AsyncSqliteStore,
        chroma_store: Optional[BaseStore] = None,
    ):
        self._sqlite = sqlite_store
        self._chroma = chroma_store
        # 校验路由表与可用后端一致
        for kind, backend in NAMESPACE_BACKEND_ROUTES.items():
            if backend == "chromadb" and chroma_store is None:
                logger.warning(
                    f"namespace kind={kind!r} 路由到 chromadb 但 chroma_store=None, "
                    f"将回退到 sqlite"
                )

    def _route(self, namespace: tuple[str, ...]) -> BaseStore:
        """namespace 第二段（kind）→ 后端."""
        kind = namespace[1] if len(namespace) >= 2 else "preferences"
        backend = NAMESPACE_BACKEND_ROUTES.get(kind, "sqlite")
        if backend == "chromadb" and self._chroma is not None:
            return self._chroma
        return self._sqlite

    # ── BaseStore 的两个抽象方法 ──────────────────────────
    # batch/abatch 是其它高级方法 (aput/aget/asearch/...) 的底层入口;
    # 这里按每个 Op 的 namespace 分组路由.

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        # 同步 batch — 仅对 Sqlite 后端有意义; 实际项目全部走 abatch
        from itertools import groupby
        ops_list = list(ops)
        results: list[Result] = [None] * len(ops_list)
        # 按 backend 实例分组
        grouped: dict[int, list[tuple[int, Op]]] = {}
        for idx, op in enumerate(ops_list):
            ns = op.namespace if hasattr(op, "namespace") else ()
            backend = self._route(ns)
            grouped.setdefault(id(backend), []).append((idx, op))
        for backend_id, items in grouped.items():
            # 找到 backend
            backend = next(b for b in (self._sqlite, self._chroma)
                           if b is not None and id(b) == backend_id)
            batch_ops = [op for _, op in items]
            batch_results = backend.batch(batch_ops)
            for (orig_idx, _), res in zip(items, batch_results):
                results[orig_idx] = res
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        ops_list = list(ops)
        results: list[Result] = [None] * len(ops_list)
        grouped: dict[int, list[tuple[int, Op]]] = {}
        for idx, op in enumerate(ops_list):
            ns = op.namespace if hasattr(op, "namespace") else ()
            backend = self._route(ns)
            grouped.setdefault(id(backend), []).append((idx, op))
        for backend_id, items in grouped.items():
            backend = next(b for b in (self._sqlite, self._chroma)
                           if b is not None and id(b) == backend_id)
            batch_ops = [op for _, op in items]
            batch_results = await backend.abatch(batch_ops)
            for (orig_idx, _), res in zip(items, batch_results):
                results[orig_idx] = res
        return results


async def build_store(
    db: Optional[AgentDB] = None,
    db_path: Optional[Path] = None,
) -> DualBackendStore:
    """构建 DualBackendStore (主 SQLite, Chroma 暂不接入).

    Args:
        db: AgentDB 实例 (优先用其 db_path)
        db_path: 直接 SQLite 文件路径

    Returns:
        DualBackendStore — 可直接传给 graph.compile(store=...)
    """
    if db is None and db_path is None:
        raise ValueError("build_store 需要 db 或 db_path 之一")
    target_path = str(db.db_path) if db is not None else str(db_path)
    # 关键: langgraph 内部用 explicit BEGIN/COMMIT, 必须 manual 模式
    # 由 connect 时传 isolation_level=None (sqlite3 / aiosqlite 都支持)
    conn = await aiosqlite.connect(target_path, isolation_level=None)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=30000")

    sqlite_store = AsyncSqliteStore(conn=conn)
    await sqlite_store.setup()
    logger.info(f"Store ready (db={target_path}, backend=sqlite)")
    return DualBackendStore(sqlite_store=sqlite_store, chroma_store=None)


async def close_store(store: DualBackendStore) -> None:
    """优雅关闭 — daemon 退出时调用."""
    try:
        if store._sqlite is not None:
            await store._sqlite.conn.close()
            logger.info("Store SQLite connection closed")
    except Exception as e:
        logger.warning(f"close_store failed: {e}")
