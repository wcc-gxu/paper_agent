"""Agent 守护进程入口 — 启动/恢复/迁移 + AgentRunLoop 事件循环。

协议参见 docs/development/agent-manifest.md §3。

架构:
  AgentBootstrap → 创建/恢复 Agent 组件
  AgentRunLoop  → PriorityQueue 消费 4 事件源
    1. WebSocket (prio=0)        → 待 ws_handler 注入
    2. RedisEventSource (prio=1~2)→ Celery Worker 进度
    3. SubAgentReportListener     → Redis Pub/Sub 子Agent 实时报告
    4. TimerEventSource (prio=3)  → 定时任务

使用方式:
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
            {"manifest": dict, "graph": CompiledStateGraph, "state": dict|None,
             "db": AgentDB, "memory": MemoryManager, "tools": ToolRegistry}
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
        import aiosqlite

        pg = PlanGraph(llm=self._llm, tools=self._tools, memory=self._memory, db=self._db)
        self._aiosqlite_conn = await aiosqlite.connect(str(db_path))
        await self._aiosqlite_conn.execute("PRAGMA journal_mode=WAL")
        await self._aiosqlite_conn.execute("PRAGMA busy_timeout=30000")
        checkpointer = AsyncSqliteSaver(conn=self._aiosqlite_conn)
        await checkpointer.setup()
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

        return {"manifest": manifest_data, "graph": self._graph, "plan_graph": pg,
                "state": state, "db": self._db, "memory": self._memory, "tools": self._tools}

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
        import aiosqlite

        pg = PlanGraph(llm=self._llm, tools=self._tools, memory=self._memory, db=self._db)
        self._aiosqlite_conn = await aiosqlite.connect(str(db_path))
        await self._aiosqlite_conn.execute("PRAGMA journal_mode=WAL")
        await self._aiosqlite_conn.execute("PRAGMA busy_timeout=30000")
        checkpointer = AsyncSqliteSaver(conn=self._aiosqlite_conn)
        await checkpointer.setup()
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

        return {"manifest": manifest_data, "graph": self._graph, "plan_graph": pg,
                "state": None, "db": self._db, "memory": self._memory, "tools": self._tools}


# ═══════════════════════════════════════════════════════════════
# AgentRunLoop — 事件驱动主循环
# ═══════════════════════════════════════════════════════════════


class AgentRunLoop:
    """事件驱动主循环 — Tick-Polling 模式 (对齐 iOS CFRunLoop)。

    源检查顺序 = 优先级:
      Source 0: WebSocket (用户交互) — pop_nowait() 逐条 ≤5/tick
      Source 1: Redis LPOP (Celery 结果) — pop_nowait() 逐条 ≤3/tick
      Source 2: Pub/Sub drain (子Agent 报告) — drain() 批量合并
      Source 3: Timer pop_fired (定时任务) — 所有到期

    所有源都空时 → sleep(TICK=16ms) → 对齐 iOS mach_msg(timeout)
    """

    TICK = 1 / 60  # 16.7ms
    MAX_WS_PER_TICK = 5
    MAX_REDIS_PER_TICK = 3

    def __init__(self, graph, db, manifest_data: dict,
                 redis_url: str = "redis://localhost:6379/0",
                 agent_id: str = "agent-001",
                 plan_graph=None):
        self._graph = graph
        self._plan_graph = plan_graph
        self._db = db
        self._manifest_data = manifest_data
        self._redis_url = redis_url
        self._agent_id = agent_id

        # 事件源 (tick-polling 模式直接持有)
        from .event_bus import WSMessageQueue
        self._ws_source = WSMessageQueue()
        self._redis_source = None
        self._report_listener = None
        self._timer_source = None

        # Observer
        from .event_bus import Observer
        self._observer = Observer()

        # 状态
        self._running = False
        self._ws_send_fn: Optional[Any] = None

    @property
    def running(self) -> bool:
        return self._running

    def set_ws_send_fn(self, send_fn):
        self._ws_send_fn = send_fn

    # ── 启动/停止 ─────────────────────────────────────

    async def start(self):
        """启动 AgentRunLoop 和所有事件源。"""
        logger.info("AgentRunLoop starting (tick-polling mode)...")
        self._running = True

        from .event_bus import RedisEventSource, SubAgentReportListener, TimerEventSource, TimerDef

        # Source 1: Redis LPOP (Celery 结果)
        self._redis_source = RedisEventSource(self._redis_url, self._agent_id)

        # Source 2: Pub/Sub (子Agent 报告)
        self._report_listener = SubAgentReportListener(self._redis_url)
        self._report_listener.start()

        # Source 3: Timer
        self._timer_source = TimerEventSource()
        self._timer_source.start()
        self._timer_source.register(TimerDef(
            name="health_check", interval_seconds=1200, timer_type="health_check"))
        self._timer_source.register(TimerDef(
            name="cleanup_logs", interval_seconds=86400, timer_type="cleanup"))

        # 绑定 report_listener 到 PlanGraph
        if self._plan_graph and hasattr(self._plan_graph, 'set_report_listener'):
            self._plan_graph.set_report_listener(self._report_listener)
            logger.info("Report listener bound to PlanGraph")

        logger.info("AgentRunLoop started — tick-polling")
        await self._run_loop()

    async def stop(self):
        """优雅关闭。"""
        logger.info("AgentRunLoop stopping...")
        self._running = False
        if self._timer_source:
            await self._timer_source.stop()
        if self._report_listener:
            self._report_listener.stop()
        logger.info("AgentRunLoop stopped")

    # ── 主循环 (Tick-Polling) ──────────────────────────

    async def _run_loop(self):
        """Tick-Polling 主循环 — 对齐 iOS CFRunLoop。

        每 tick: 按优先级顺序检查每个源
          - WS:   逐条 ≤5/tick
          - Redis: 逐条 ≤3/tick
          - PubSub: drain() 批量合并
          - Timer:  pop_fired() 所有到期
          - Observer: 心跳超时检查
          - 全部空 → sleep(TICK)
        """
        while self._running:
            handled = False

            # ═══ Source 0: WebSocket (用户交互) ═══
            for _ in range(self.MAX_WS_PER_TICK):
                msg = self._ws_source.pop_nowait()
                if msg is None:
                    break
                await self._dispatch_ws_message(msg)
                handled = True

            # ═══ Source 1: Redis LPOP (Celery 完成/错误) ═══
            for _ in range(self.MAX_REDIS_PER_TICK):
                event = self._redis_source.pop_nowait()
                if event is None:
                    break
                await self._handle_celery_result(event)
                handled = True

            # ═══ Source 2: Pub/Sub (子Agent 实时报告) ═══
            reports = self._report_listener.drain()
            if reports:
                merged = self._merge_reports(reports)
                for report in merged:
                    await self._handle_report(report)
                handled = True

            # ═══ Source 3: Timer (定时任务) ═══
            fired = self._timer_source.pop_fired()
            for timer in fired:
                await self._handle_timer(timer)
            if fired:
                handled = True

            # ═══ Observer: 心跳超时检查 ═══
            timed_out = self._observer.check_timeouts()
            for tid in timed_out:
                await self._handle_sub_agent_lost(tid)

            # ═══ 休眠 (对齐 iOS mach_msg(timeout)) ═══
            if not handled:
                await asyncio.sleep(self.TICK)

    # ── 数据批处理 ─────────────────────────────────────

    @staticmethod
    def _merge_reports(reports: list) -> list:
        """同一 task_id 的进度合并为一条 summary，保留最新 stage/进度。"""
        from .event_bus import ProgressReport
        by_task: dict[str, ProgressReport] = {}
        lifecycles = []
        for r in reports:
            if r.is_lifecycle:
                lifecycles.append(r)
                continue
            tid = r.task_id
            if tid not in by_task:
                by_task[tid] = r
            else:
                # 保留最新的进度
                existing = by_task[tid]
                if r.paper_index > existing.paper_index:
                    existing.paper_index = r.paper_index
                    existing.paper_total = r.paper_total
                    existing.stage = r.stage or existing.stage
                    existing.stage_index = r.stage_index or existing.stage_index
                    existing.status = r.status or existing.status
        return lifecycles + list(by_task.values())

    # ── 分发 ──────────────────────────────────────────

    async def _dispatch_ws_message(self, msg: dict):
        """分发 WebSocket 消息到 PlanGraph。"""
        from .event_bus import EventType
        sub_type = msg.get("subType", "")
        payload = msg.get("payload", {})
        session_id = msg.get("sessionId", "main")

        if sub_type == "chat":
            await self._handle_user_message(
                session_id, payload.get("text", ""), msg)
        elif sub_type == "clarify":
            await self._handle_clarification(
                session_id, payload.get("answers", []))
        elif sub_type == "approve":
            await self._handle_approval(
                session_id, payload.get("confirmed", False),
                payload.get("modifications", {}))

    # ── 事件处理器 ──────────────────────────────────────

    async def _handle_user_message(self, session_id: str, content: str, raw: dict):
        config = {"configurable": {"thread_id": f"{self._agent_id}-plan-{session_id}"}}
        try:
            result = await self._graph.ainvoke(
                {"messages": [{"role": "user", "content": content}]},
                config=config)
            await self._dispatch_graph_output(result, session_id)
        except Exception as e:
            logger.error(f"PlanGraph invocation failed: {e}", exc_info=True)

    async def _handle_clarification(self, session_id: str, answers: list):
        config = {"configurable": {"thread_id": f"{self._agent_id}-plan-{session_id}"}}
        try:
            result = await self._graph.aresume(
                config, {"user_clarification": {"answers": answers}})
            await self._dispatch_graph_output(result, session_id)
        except Exception as e:
            logger.error(f"PlanGraph aresume (clarify) failed: {e}", exc_info=True)

    async def _handle_approval(self, session_id: str, confirmed: bool, modifications: dict):
        config = {"configurable": {"thread_id": f"{self._agent_id}-plan-{session_id}"}}
        try:
            result = await self._graph.aresume(
                config, {"user_approval": {"confirmed": confirmed,
                                           "modifications": modifications}})
            await self._dispatch_graph_output(result, session_id)
        except Exception as e:
            logger.error(f"PlanGraph aresume (approval) failed: {e}", exc_info=True)

    async def _handle_celery_result(self, event):
        """处理 Celery 完成/错误。"""
        from .event_bus import CeleryResultEvent
        if event.is_error:
            logger.error(f"Celery error: task={event.agent_task_id} err={event.error[:200]}")
            if self._ws_send_fn:
                await self._ws_send_fn({
                    "role": "assistant", "type": "task", "subType": "failed",
                    "agentId": self._agent_id, "sessionId": "main",
                    "seq": 0, "priority": 1, "timestamp": _now(),
                    "payload": {"taskId": event.agent_task_id, "error": event.error},
                })
        else:
            logger.info(f"Celery done: task={event.agent_task_id}")
            if self._ws_send_fn:
                await self._ws_send_fn({
                    "role": "assistant", "type": "task", "subType": "done",
                    "agentId": self._agent_id, "sessionId": "main",
                    "seq": 0, "priority": 1, "timestamp": _now(),
                    "payload": {"taskId": event.agent_task_id, "result": event.result},
                })

    async def _handle_report(self, report):
        """处理子Agent 报告 (含生命周期 + 进度)。"""
        from .event_bus import ProgressReport
        tid = report.task_id

        if report.is_lifecycle:
            lt = report.lifecycle_type
            if lt == "started":
                self._observer.on_agent_started(tid, report.agent_type)
            elif lt == "done":
                self._observer.on_agent_done(tid)
                if self._report_listener:
                    await self._report_listener.unsubscribe(tid)
            elif lt == "failed":
                self._observer.on_agent_failed(tid, report.data.get("error", ""))
                if self._report_listener:
                    await self._report_listener.unsubscribe(tid)
        else:
            self._observer.on_agent_report(tid)

        # WS 推送进度
        if self._ws_send_fn:
            await self._ws_send_fn({
                "role": "assistant", "type": "task", "subType": "running",
                "agentId": self._agent_id, "sessionId": "main",
                "seq": 0, "priority": 2, "timestamp": _now(),
                "payload": {
                    "taskId": tid,
                    "stage": report.stage,
                    "stageIndex": report.stage_index,
                    "totalStages": report.total_stages,
                    "current": report.paper_index,
                    "total": report.paper_total,
                },
            })

    async def _handle_timer(self, timer):
        """处理定时触发事件。"""
        logger.info(f"Timer fired: {timer.timer_name} (type={timer.timer_type})")
        if timer.timer_type == "health_check":
            await self._run_health_check()
        elif timer.timer_type == "cleanup":
            await self._run_cleanup()

    async def _handle_sub_agent_lost(self, task_id: str):
        """处理子Agent 心跳超时。"""
        self._observer.on_agent_failed(task_id, "heartbeat_timeout")
        if self._report_listener:
            await self._report_listener.unsubscribe(task_id)
        if self._ws_send_fn:
            await self._ws_send_fn({
                "role": "assistant", "type": "task", "subType": "failed",
                "agentId": self._agent_id, "sessionId": "main",
                "seq": 0, "priority": 1, "timestamp": _now(),
                "payload": {"taskId": task_id, "error": "Sub-agent lost (heartbeat timeout)"},
            })

    async def _dispatch_graph_output(self, result, session_id: str):
        """将 PlanGraph 输出转为 WS 消息。"""
        if result is None:
            return
        pending_review = result.get("pending_review")
        if pending_review and isinstance(pending_review, dict):
            await self._send_ws({
                "role": "assistant",
                "type": pending_review.get("type", "review"),
                "subType": pending_review.get("subType", "clarify"),
                "agentId": self._agent_id, "sessionId": session_id,
                "seq": 0, "priority": 0, "timestamp": _now(),
                "payload": pending_review.get("payload", {}),
            })

        plan_status = result.get("plan_status", "")
        if plan_status:
            await self._send_ws({
                "role": "assistant", "type": "phase",
                "subType": plan_status, "agentId": self._agent_id,
                "sessionId": session_id, "seq": 0, "priority": 0,
                "timestamp": _now(), "payload": {"planStatus": plan_status},
            })

        error = result.get("error")
        if error:
            await self._send_ws({
                "role": "assistant", "type": "error", "subType": "error",
                "agentId": self._agent_id, "sessionId": session_id,
                "seq": 0, "priority": 0, "timestamp": _now(),
                "payload": {"message": str(error)},
            })

    async def _send_ws(self, envelope: dict):
        if self._ws_send_fn:
            try:
                await self._ws_send_fn(envelope)
            except Exception as e:
                logger.error(f"WS send failed: {e}")

    async def _run_health_check(self):
        status = {}
        try:
            self._db.conn.execute("SELECT 1")
            status["db"] = "ok"
        except Exception as e:
            status["db"] = f"error: {e}"
        if self._redis_source:
            try:
                self._redis_source.redis.ping()
                status["redis"] = "ok"
            except Exception as e:
                status["redis"] = f"error: {e}"
        logger.info(f"Health check: {json.dumps(status)}")

    async def _run_cleanup(self):
        log_dir = Path.home() / ".paper_search" / "logs" / "sub_agents"
        if log_dir.exists():
            cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400
            count = 0
            for f in log_dir.glob("**/*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    count += 1
            if count:
                logger.info(f"Cleanup: removed {count} old task logs")


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


async def main(data_dir: Optional[str] = None, redis_url: Optional[str] = None,
               new_loop: bool = False):
    """daemon 主入口 — bootstrap + AgentRunLoop。

    启动流程:
      1. AgentBootstrap.bootstrap() → 创建/恢复 Agent 组件
      2. 创建 AgentRunLoop (v1) 或 AgentLoop (v2)
      3. 启动 RunLoop (阻塞直到 SIGTERM/SIGINT)

    Args:
        data_dir: 数据目录
        redis_url: Redis URL
        new_loop: True = 启动新 AgentLoop (v2), False = 原 AgentRunLoop (v1)
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    path = Path(data_dir) if data_dir else None
    redis = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Bootstrap
    bs = AgentBootstrap(data_dir=path)
    result = await bs.bootstrap()

    manifest = result["manifest"]
    agent_id = manifest["agent"]["agent_id"]

    logger.info(f"Agent {agent_id} ready")
    logger.info(f"  Status: {manifest['agent']['status']}")
    logger.info(f"  Graph: {type(result['graph']).__name__}")
    logger.info(f"  State: {'restored' if result['state'] else 'fresh'}")

    if new_loop:
        # ── v2: AgentLoop (WebSocket 驱动) ──
        from .agent_loop import AgentLoop
        from .llm_client_v2 import LLMClientV2
        from .tool_registry import ToolRegistry

        llm = LLMClientV2()
        tools = ToolRegistry.get_instance()

        agent_loop = AgentLoop(
            agent_id=agent_id,
            redis_url=redis,
            llm=llm,
            db=result["db"],
            tools=tools.get_chat_tools() if hasattr(tools, "get_chat_tools") else [],
        )
        logger.info("AgentLoop v2 starting (WebSocket-driven)...")
        await agent_loop.run()
        return result

    # ── v1: AgentRunLoop (tick-polling) ──
    # 创建 AgentRunLoop (tick-polling 模式)
    loop = AgentRunLoop(
        graph=result["graph"],
        db=result["db"],
        manifest_data=manifest,
        redis_url=redis,
        agent_id=agent_id,
        plan_graph=result.get("plan_graph"),
    )

    # 注册信号处理
    def _sig_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(loop.stop())

    try:
        loop_evt = asyncio.get_event_loop()
    except RuntimeError:
        loop_evt = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop_evt.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:
            pass  # Windows 不支持 add_signal_handler

    # 启动 RunLoop（阻塞）
    try:
        await loop.start()
    except asyncio.CancelledError:
        pass

    return result


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    asyncio.run(main())
