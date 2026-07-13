"""Agent 守护进程入口 — bootstrap + MainAgent。

Phase 5: 简化版守护进程
  - AgentManifest: Agent 身份持久化（保留）
  - AgentBootstrap: 创建/恢复 db + llm + tools + memory（不再编译 PlanGraph）
  - 启动 MainAgent (5 节点显式状态机) 主循环

旧 AgentRunLoop 类已删除（事件循环现在在 MainAgent.run 里）。

使用方式::

    python -m paper_search.agent.daemon
    python -m paper_search.agent.daemon --data-dir /path
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_AGENT_ID = "agent-001"
DEFAULT_DISPLAY_NAME = "Paper Agent"
MANIFEST_VERSION = "1.0"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════════
# Manifest
# ═══════════════════════════════════════════════════════════════


class AgentManifest:
    """Agent 身份证 — JSON 序列化数据结构。

    v3 Phase 1: 支持多用户，每个 user_id 独立 manifest 文件。
    """

    def __init__(self, data_dir: Path, user_id: str = "default"):
        self.user_id = user_id
        manifests_dir = data_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        self.path = manifests_dir / f"{user_id}.json"
        self.data: dict = {}
        self._legacy_path = data_dir / "agent_manifest.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        # 向后兼容：从旧 manifest 路径迁移
        if not self.path.exists() and self._legacy_path.exists():
            logger.info(f"Migrating legacy manifest from {self._legacy_path} to {self.path}")
            old_data = json.loads(self._legacy_path.read_text(encoding="utf-8"))
            old_data.setdefault("agent", {})["agent_id"] = f"agent-{self.user_id}"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(old_data, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            # 保留旧文件作为备份，不删除
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        return self.data

    def save(self, data: dict):
        self.data = data
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        logger.info(f"Manifest saved: {self.path}")

    @property
    def agent_id(self) -> str:
        return (self.data.get("agent") or {}).get("agent_id", f"agent-{self.user_id}")

    @property
    def llm_provider(self) -> str:
        return ((self.data.get("runtime") or {}).get("llm") or {}).get("provider", "deepseek")


# ═══════════════════════════════════════════════════════════════
# Bootstrap
# ═══════════════════════════════════════════════════════════════


class AgentBootstrap:
    """Agent 引导程序 — 启动时调用的唯一入口。

    v3 Phase 1 重构：支持多用户（user_id 参数）。

    构建 4 个核心组件:
      - AgentDB / PostgresAgentDB
      - LLMClientV2
      - ToolRegistry
      - MemoryManager (含 ChromaStore/PgVectorStore 可选)
    """

    def __init__(self, data_dir: Optional[Path] = None, user_id: str = "default"):
        if data_dir is None:
            from ..config import get_data_dir
            data_dir = get_data_dir()
        self.data_dir = Path(data_dir)
        self.user_id = user_id
        self.manifest = AgentManifest(self.data_dir, user_id=user_id)

        # 惰性初始化的组件
        self._db = None
        self._llm = None
        self._tools = None
        self._memory = None

    async def bootstrap(self) -> dict:
        """主入口 — 创建或恢复 Agent。

        Returns:
            {"manifest": dict, "db": AgentDB, "llm": LLMClientV2,
             "tools": ToolRegistry, "memory": MemoryManager}
        """
        if self.manifest.exists():
            logger.info(f"Resuming agent from manifest: {self.manifest.path}")
            return await self._resume()
        logger.info("First boot — creating main agent")
        return await self._create()

    # ── 核心组件构建（创建/恢复共用） ─────────────────

    async def _build_core(self, manifest_data: dict, llm_provider: str = "deepseek"):
        """统一构建 db / llm / tools / memory，避免 _create/_resume 重复代码。"""
        from ..config import use_postgresql

        # 1. DB: PostgreSQL 或 SQLite
        if use_postgresql():
            from ..agent.pgdb import PostgresAgentDB
            self._db = PostgresAgentDB()
            logger.info("Database: PostgreSQL")
        else:
            from ..agent.db import AgentDB
            db_path = self.data_dir / "agent.db"
            self._db = AgentDB(db_path)
            # 触发 schema + migrations
            _ = self._db.conn
            logger.info(f"Database: SQLite ({db_path})")

        # 2. LLMClientV2
        from ..agent.llm_client_v2 import LLMClientV2
        self._llm = LLMClientV2(provider=llm_provider)
        logger.info("LLM client initialized")

        # 3. ToolRegistry
        from ..agent.tool_registry import ToolRegistry
        self._tools = ToolRegistry.get_instance()
        logger.info(f"ToolRegistry: {len(self._tools.tool_names)} tools")

        # 4. MemoryManager (含向量存储, 可选)
        from ..agent.memory import MemoryManager
        chroma = None
        try:
            if use_postgresql():
                from ..agent.pgvector_store import PgVectorStore
                chroma = PgVectorStore(user_id=self.user_id)
                logger.info("PgVectorStore initialized")
            else:
                from ..agent.chroma_store import ChromaStoreV2
                chroma = ChromaStoreV2()
                logger.info("ChromaDB initialized")
        except Exception as e:
            logger.warning(f"Vector store unavailable (knowledge tools degrade): {e}")
        self._memory = MemoryManager(self._db, chroma)

    async def _resume(self) -> dict:
        """从已有的 manifest 恢复 Agent。"""
        manifest_data = self.manifest.load()
        llm_provider = ((manifest_data.get("runtime") or {}).get("llm") or {}).get("provider", "deepseek")
        await self._build_core(manifest_data, llm_provider=llm_provider)
        # 更新 manifest 状态
        manifest_data.setdefault("agent", {})["status"] = "active"
        manifest_data["agent"]["updated_at"] = _now()
        self.manifest.save(manifest_data)
        return {
            "manifest": manifest_data,
            "db": self._db, "llm": self._llm,
            "tools": self._tools, "memory": self._memory,
        }

    async def _create(self) -> dict:
        """首次启动 — 从头创建 Agent。"""
        now = _now()
        agent_id = f"agent-{self.user_id}" if self.user_id != "default" else DEFAULT_AGENT_ID

        manifest_data = {
            "manifest_version": MANIFEST_VERSION,
            "agent": {
                "agent_id": agent_id,
                "type": "main",
                "display_name": DEFAULT_DISPLAY_NAME,
                "user_id": self.user_id,
                "created_at": now,
                "updated_at": now,
                "status": "active",
            },
            "runtime": {
                "llm": {"provider": "deepseek"},
            },
        }
        await self._build_core(manifest_data, llm_provider="deepseek")
        self._db.create_session(agent_id, "main", title="新对话", user_id=self.user_id)
        self.manifest.save(manifest_data)
        return {
            "manifest": manifest_data,
            "db": self._db, "llm": self._llm,
            "tools": self._tools, "memory": self._memory,
        }


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


async def main(data_dir: Optional[str] = None, redis_url: Optional[str] = None,
               user_id: str = "default"):
    """daemon 主入口 — bootstrap + MainAgent。

    Args:
        data_dir: 数据目录路径。
        redis_url: Redis 连接 URL。
        user_id: 用户 ID（v3 多用户支持）。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Enable debug protocol: pushes LLM thinking + tool call details to WS client
    os.environ.setdefault("DEBUG_PROTOCOL", "1")
    logger.info("DEBUG_PROTOCOL enabled — LLM thinking + tool calls will be pushed to clients")

    path = Path(data_dir) if data_dir else None
    redis = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # 从环境变量读取 user_id（可选覆盖）
    user_id = os.getenv("PAPER_USER_ID", user_id)

    # Bootstrap
    bs = AgentBootstrap(data_dir=path, user_id=user_id)
    result = await bs.bootstrap()

    manifest = result["manifest"]
    agent_id = manifest["agent"]["agent_id"]
    logger.info(f"Agent {agent_id} (user={user_id}) ready, starting MainAgent...")

    # v3.1: Build LangGraph MainGraph
    from .graphs.main_graph import build_main_graph
    from .outbox import outbox_publish
    import redis.asyncio as aioredis

    # Create Redis client for graph outbox pushes
    _redis_client = aioredis.from_url(redis, decode_responses=True)

    async def _graph_push(session_id: str, msg_type: str, subtype: str,
                          role: str, payload: dict = None,
                          priority_kind: str = "normal") -> None:
        """Adapter: graph node push → outbox_publish."""
        envelope = {
            "type": msg_type, "subType": subtype, "role": role,
            "agentId": agent_id, "sessionId": session_id,
            "payload": payload or {},
            "priority": priority_kind,
        }
        await outbox_publish(_redis_client, result["db"], envelope)

    async def _graph_get_user(session_id: str, ask_id: str,
                               timeout: int = 1800) -> dict | None:
        """Wait for user reply to an ask card via Redis BRPOP.

        Matches messages of type 'ask_reply' with matching session_id and ask_id.
        Non-matching messages are parked in the parked queue for later processing.
        Returns None on timeout.
        """
        ws_queue = f"agent:ws:{agent_id}"
        parked_queue = f"agent:ws:{agent_id}:parked"
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.info("get_user timed out for ask_id=%s", ask_id)
                return None
            try:
                raw = await _redis_client.brpop(
                    ws_queue, timeout=int(min(remaining, 30)),
                )
            except Exception:
                await asyncio.sleep(0.5)
                continue
            if raw is None:
                continue
            try:
                msg = json.loads(raw[1])
            except json.JSONDecodeError:
                continue

            # Match: ask_reply type, matching session and ask_id
            p = msg.get("payload") or {}
            if (msg.get("type") == "ask_reply"
                    and msg.get("_session_id") == session_id
                    and p.get("ask_id") == ask_id):
                return p

            # Not matching — park it for the main loop to pick up later
            try:
                await _redis_client.lpush(parked_queue, raw[1])
            except Exception:
                pass

    compiled_graph = build_main_graph(
        llm=result["llm"],
        registry=result["tools"],
        db=result["db"],
        push_fn=_graph_push,
        get_user_fn=_graph_get_user,
    )

    # MainAgent (v3.1: delegates to compiled_graph)
    from .main_agent import MainAgent
    main_agent = MainAgent(
        agent_id=agent_id,
        redis_url=redis,
        llm=result["llm"],
        db=result["db"],
        memory=result["memory"],
        registry=result["tools"],
        graph=compiled_graph,
    )

    # 信号处理（优雅退出）
    stop_event = asyncio.Event()

    def _sig_handler():
        logger.info("Received shutdown signal")
        stop_event.set()

    try:
        loop_evt = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop_evt.add_signal_handler(sig, _sig_handler)
            except (NotImplementedError, RuntimeError):
                pass  # Windows 不支持
    except RuntimeError:
        pass

    run_task = asyncio.create_task(main_agent.run(), name="main-agent-run")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-waiter")

    try:
        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        pass
    finally:
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass

    logger.info("MainAgent stopped, exiting daemon.")
    return result


if __name__ == "__main__":
    # 简单 argv 解析（不引入 argparse 依赖）
    data_dir = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--data-dir" and i + 1 < len(sys.argv) - 1:
            data_dir = sys.argv[i + 2]
            break
        if arg.startswith("--data-dir="):
            data_dir = arg.split("=", 1)[1]
            break

    asyncio.run(main(data_dir=data_dir))
