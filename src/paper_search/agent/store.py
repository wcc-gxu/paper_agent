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


# ═══════════════════════════════════════════════════════════════
# Episode Manager — 会话历史管理 (v3 Phase 2: 吸收 history_graph)
# ═══════════════════════════════════════════════════════════════


class EpisodeManager:
    """会话 Episodes 管理器 — 替代原 HistoryAgent。

    功能:
      - 会话摘要归档至 Store episodes namespace
      - 重复消息合并
      - 过期消息清理
      - 待办项生成与通知

    用法:
        mgr = EpisodeManager(store, db)
        await mgr.archive_session(agent_id, session_id)
        summary = await mgr.get_session_summary(agent_id, session_id)
    """

    def __init__(self, store: DualBackendStore, db=None):
        self.store = store
        self.db = db
        # 默认保留: 7 天内的 episodes
        self.max_age_days = 7
        self.max_episodes_per_session = 50

    async def archive_session(self, agent_id: str, session_id: str) -> dict:
        """归档会话 — 将当前 session 的 episodes 写入 Store。

        流程 (替代原 HistoryAgent.archive 节点):
          1. 读取 session 的消息列表
          2. 合并重复/相似消息
          3. 生成会话摘要
          4. 写入 Store (agent_id, "episodes", session_id)
          5. 清理过期 episodes
        """
        messages = []
        if self.db:
            try:
                raw = self.db.get_session_messages(agent_id, session_id)
                messages = raw if raw else []
            except Exception as e:
                logger.warning(f"Failed to load session messages: {e}")

        if not messages:
            return {"archived": 0, "session_id": session_id}

        # 合并重复消息
        merged = self._merge_duplicates(messages)

        # 生成摘要
        summary = self._summarize_messages(merged)

        # 写入 Store
        import time
        episode_data = {
            "session_id": session_id,
            "message_count": len(messages),
            "merged_count": len(merged),
            "summary": summary,
            "last_message_at": messages[-1].get("created_at", ""),
            "archived_at": time.time(),
        }

        await self.store.aput(
            (agent_id, "episodes", session_id),
            f"episode_{session_id}",
            episode_data,
        )

        logger.info(f"Archived session {session_id}: {len(messages)} msgs → {len(merged)} merged")
        return {"archived": len(merged), "session_id": session_id}

    async def get_session_summary(self, agent_id: str, session_id: str) -> dict:
        """获取会话摘要。"""
        items = await self.store.asearch(
            (agent_id, "episodes", session_id),
            limit=1,
        )
        if items:
            return items[0].value if hasattr(items[0], "value") else items[0]
        return {}

    async def cleanup_old_episodes(self, agent_id: str):
        """清理超过 max_age_days 的旧 episodes。"""
        import time
        cutoff = time.time() - (self.max_age_days * 86400)
        # 搜索旧 episodes
        items = await self.store.asearch(
            (agent_id, "episodes"),
            limit=self.max_episodes_per_session,
        )
        deleted = 0
        for item in items:
            val = item.value if hasattr(item, "value") else item
            if isinstance(val, dict) and val.get("archived_at", 0) < cutoff:
                await self.store.adelete(
                    (agent_id, "episodes", val.get("session_id", "")),
                    item.key if hasattr(item, "key") else "",
                )
                deleted += 1

        if deleted:
            logger.info(f"Cleaned up {deleted} old episodes for agent={agent_id}")
        return deleted

    def _merge_duplicates(self, messages: list[dict]) -> list[dict]:
        """合并重复/相似消息（替代原 HistoryAgent.merge 节点）。"""
        seen = set()
        merged = []
        for msg in messages:
            key = (
                msg.get("role", ""),
                msg.get("type", ""),
                str(msg.get("payload", ""))[:200],
            )
            if key not in seen:
                seen.add(key)
                merged.append(msg)
        return merged

    def _summarize_messages(self, messages: list[dict]) -> dict:
        """简单汇总消息统计（摘要由 LLM 在档 2 完成，这里只统计）。"""
        roles = {}
        types = {}
        for m in messages:
            role = m.get("role", "unknown")
            roles[role] = roles.get(role, 0) + 1
            t = m.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
        return {
            "total": len(messages),
            "roles": roles,
            "types": types,
            "first_at": messages[0].get("created_at", "") if messages else "",
            "last_at": messages[-1].get("created_at", "") if messages else "",
        }

