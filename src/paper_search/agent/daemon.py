"""Agent 守护进程入口 — 启动/恢复/迁移。

协议参见 docs/development/agent-manifest.md §3。

使用方式:
    python -m paper_search.agent.daemon                   # 首次启动
    python -m paper_search.agent.daemon --data-dir /path   # 指定数据目录
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_AGENT_ID = "agent-001"
DEFAULT_DISPLAY_NAME = "Paper Agent"
MANIFEST_VERSION = "1.0"


# ═══════════════════════════════════════════════════════════════
# Manifest
# ═══════════════════════════════════════════════════════════════


class AgentManifest:
    """Agent 身份证 — JSON 序列化数据结构。"""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "agent_manifest.json"
        self.data: dict = {}

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self.data

    def save(self, data: dict):
        self.data = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"Manifest saved: {self.path}")

    # ── 便捷属性 ──────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return (self.data.get("agent") or {}).get("agent_id", DEFAULT_AGENT_ID)

    @property
    def llm_provider(self) -> str:
        return ((self.data.get("runtime") or {}).get("llm") or {}).get("provider", "volcano")

    @property
    def thread_id(self) -> str:
        return ((self.data.get("runtime") or {}).get("plan_graph") or {}).get("thread_id", f"{self.agent_id}-plan")


# ═══════════════════════════════════════════════════════════════
# Bootstrap
# ═══════════════════════════════════════════════════════════════


class AgentBootstrap:
    """Agent 引导程序 — 启动时调用的唯一入口。"""

    def __init__(self, data_dir: Optional[Path] = None):
        if data_dir is None:
            from ..config import get_data_dir
            data_dir = get_data_dir()
        self.data_dir = Path(data_dir)
        self.manifest = AgentManifest(self.data_dir)

        # 惰性初始化的组件
        self._db = None
        self._llm = None
        self._tools = None
        self._memory = None
        self._graph = None

    async def bootstrap(self) -> dict:
        """主入口 — 创建或恢复 Agent。

        Returns:
            {"manifest": dict, "graph": CompiledStateGraph, "state": dict|None}
        """
        if self.manifest.exists():
            logger.info(f"Resuming agent from manifest: {self.manifest.path}")
            return await self._resume()
        else:
            logger.info("First boot — creating main agent")
            return await self._create()

    # ── 恢复 ─────────────────────────────────────────────

    async def _resume(self) -> dict:
        """从已有的 manifest + checkpoint 恢复 Agent。"""
        manifest_data = self.manifest.load()

        # 1. 初始化数据库
        from ..agent.db import AgentDB
        db_path = self.data_dir / "agent.db"
        self._db = AgentDB(db_path)

        # 2. 初始化 LLM
        from ..agent.llm_client_v2 import LLMClientV2
        llm_config = (manifest_data.get("runtime") or {}).get("llm") or {}
        self._llm = LLMClientV2(provider=llm_config.get("provider", "volcano"))

        # 3. 初始化 ToolRegistry
        from ..agent.tool_registry import ToolRegistry
        self._tools = ToolRegistry.get_instance()

        # 4. 初始化 MemoryManager
        from ..agent.memory import MemoryManager
        try:
            from ..agent.chroma_store import ChromaStoreV2
            chroma = ChromaStoreV2()
        except Exception as e:
            logger.warning(f"ChromaDB unavailable: {e}")
            chroma = None
        self._memory = MemoryManager(self._db, chroma)

        # 5. 编译 PlanGraph
        from ..agent.graphs.plan_graph import PlanGraph
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        pg = PlanGraph(llm=self._llm, tools=self._tools, memory=self._memory, db=self._db)
        checkpointer = AsyncSqliteSaver(conn=str(db_path))
        self._graph = pg.compile(checkpointer=checkpointer)

        # 6. 从 checkpoint 恢复状态
        thread_id = self.manifest.thread_id
        config = {"configurable": {"thread_id": thread_id}}
        state = await self._graph.aget_state(config)
        logger.info(f"Resumed graph state for thread {thread_id}: {state is not None}")

        # 7. 更新 manifest
        manifest_data.setdefault("agent", {})["status"] = "active"
        manifest_data["agent"]["updated_at"] = _now()
        self.manifest.save(manifest_data)

        return {"manifest": manifest_data, "graph": self._graph, "state": state, "db": self._db, "memory": self._memory, "tools": self._tools}

    # ── 首次创建 ────────────────────────────────────────

    async def _create(self) -> dict:
        """首次启动 — 从头创建 Agent。"""
        now = _now()
        agent_id = DEFAULT_AGENT_ID

        # 1. 初始化空白数据库
        from ..agent.db import AgentDB
        db_path = self.data_dir / "agent.db"
        self._db = AgentDB(db_path)
        self._db.create_session(agent_id, "main", title="新对话")
        logger.info("Database initialized")

        # 2. 初始化 LLM
        from ..agent.llm_client_v2 import LLMClientV2
        self._llm = LLMClientV2()
        logger.info("LLM client initialized")

        # 3. 初始化 ToolRegistry
        from ..agent.tool_registry import ToolRegistry
        self._tools = ToolRegistry.get_instance()
        logger.info(f"ToolRegistry: {len(self._tools.tool_names)} tools")

        # 4. 初始化 MemoryManager
        from ..agent.memory import MemoryManager
        try:
            from ..agent.chroma_store import ChromaStoreV2
            chroma = ChromaStoreV2()
            logger.info("ChromaDB initialized")
        except Exception as e:
            logger.warning(f"ChromaDB unavailable: {e}")
            chroma = None
        self._memory = MemoryManager(self._db, chroma)

        # 5. 编译 PlanGraph
        from ..agent.graphs.plan_graph import PlanGraph
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        pg = PlanGraph(llm=self._llm, tools=self._tools, memory=self._memory, db=self._db)
        checkpointer = AsyncSqliteSaver(conn=str(db_path))
        self._graph = pg.compile(checkpointer=checkpointer)
        logger.info("PlanGraph compiled")

        # 6. 写入 manifest
        manifest_data = {
            "manifest_version": MANIFEST_VERSION,
            "agent": {
                "agent_id": agent_id,
                "type": "main",
                "display_name": DEFAULT_DISPLAY_NAME,
                "created_at": now,
                "updated_at": now,
                "status": "active",
            },
            "owner": {
                "user_id": os.getenv("AGENT_USER_ID", "user-default"),
                "bound_since": now,
            },
            "runtime": {
                "plan_graph": {
                    "module": "paper_search.agent.graphs.plan_graph",
                    "class": "PlanGraph",
                    "thread_id": f"{agent_id}-plan",
                },
                "checkpoint": {
                    "backend": "sqlite",
                    "path": str(db_path),
                    "table": "langgraph_checkpoints",
                },
                "llm": {
                    "provider": "volcano",
                    "model": os.getenv("LLM_DEFAULT_MODEL", "deepseek-v4-pro"),
                    "base_url": os.getenv("LLM_BASE_URL", ""),
                },
            },
            "memory": {
                "short_term": {"max_tokens": 16000},
                "mid_term": {"db_path": str(db_path)},
                "long_term": {
                    "chroma_path": str(self.data_dir / "chroma"),
                    "collections": [
                        "papers_abstract",
                        "papers_fulltext",
                        "agent_conversations",
                        "agent_knowledge",
                        "agent_expressions",
                        "agent_learnings",
                    ],
                },
                "meta_memory": {
                    "sqlite_tables": ["strategy_log", "error_patterns", "user_preferences"],
                },
            },
            "sessions": {
                "default": "main",
                "active": ["main"],
            },
            "data": {
                "base_dir": str(self.data_dir),
                "db_path": str(db_path),
            },
        }
        self.manifest.save(manifest_data)
        logger.info(f"Manifest created: {self.manifest.path}")

        return {"manifest": manifest_data, "graph": self._graph, "state": None, "db": self._db, "memory": self._memory, "tools": self._tools}


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


async def main(data_dir: Optional[str] = None):
    """daemon 主入口。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    path = Path(data_dir) if data_dir else None
    bs = AgentBootstrap(data_dir=path)
    result = await bs.bootstrap()

    logger.info(f"Agent {result['manifest']['agent']['agent_id']} ready")
    logger.info(f"  Status: {result['manifest']['agent']['status']}")
    logger.info(f"  Graph: {type(result['graph']).__name__}")
    logger.info(f"  State: {'restored' if result['state'] else 'fresh'}")

    return result


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
