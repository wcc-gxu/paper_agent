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

        pg = PlanGraph(llm=self._llm, tools=self._tools, memory=self._memory, db=self._db)
        checkpointer = AsyncSqliteSaver.from_conn_string(str(db_path))
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

        pg = PlanGraph(llm=self._llm, tools=self._tools, memory=self._memory, db=self._db)
        checkpointer = AsyncSqliteSaver.from_conn_string(str(db_path))
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
    """事件驱动主循环 — PriorityQueue + 4 事件源。

        ┌──────────────────────────────────────────┐
        │        AgentRunLoop (PriorityQueue)       │
        │                                          │
        │  4 个事件源 (后台协程，互不阻塞):          │
        │  ┌──────────┐ ┌───────────┐ ┌─────────┐  │
        │  │_ws_source│ │_redis_src │ │_timer   │  │
        │  │(prio=0)  │ │(prio=1~2) │ │(prio=3) │  │
        │  └────┬─────┘ └─────┬─────┘ └────┬────┘  │
        │       │              │            │       │
        │       └──────────────┴────────────┘       │
        │                     │                     │
        │                     ▼                     │
        │         while running:                    │
        │           priority, seq, event = queue    │
        │           dispatch(event)                 │
        └──────────────────────────────────────────┘
    """

    def __init__(self, graph, bus, db, manifest_data: dict,
                 redis_url: str = "redis://localhost:6379/0",
                 agent_id: str = "agent-001",
                 plan_graph=None):
        self._graph = graph               # CompiledStateGraph
        self._plan_graph = plan_graph     # PlanGraph 实例 (用于 set_report_listener)
        self._bus = bus
        self._db = db
        self._manifest_data = manifest_data
        self._redis_url = redis_url
        self._agent_id = agent_id

        # 事件源
        self._redis_source = None
        self._report_listener = None
        self._timer_source = None
        self._ws_source = None  # 由 ws_handler 通过 set_ws_source() 注入

        # 状态
        self._running = False
        self._shutdown_event = asyncio.Event()

        # WS 回调 (由 ws_handler 设置)
        self._ws_send_fn: Optional[Any] = None

    @property
    def running(self) -> bool:
        return self._running

    def set_ws_source(self, ws_queue: asyncio.Queue):
        """由 ws_handler 注入 WebSocket 消息队列。

        ws_handler 将接收到的 WS 消息放入此队列，
        AgentRunLoop 从中取出并投递到 EventBus (prio=0)。
        """
        self._ws_source = ws_queue
        logger.info("WebSocket source injected")

    def set_ws_send_fn(self, send_fn):
        """注入 WebSocket 发送回调。"""
        self._ws_send_fn = send_fn

    async def start(self):
        """启动 AgentRunLoop 和所有事件源。"""
        logger.info("AgentRunLoop starting...")
        self._running = True

        # 1. 启动 EventBus
        await self._bus.start()

        # 2. 启动 Redis 事件源 (Celery Worker 进度)
        from .event_bus import RedisEventSource, SubAgentReportListener, TimerEventSource, TimerDef
        self._redis_source = RedisEventSource(
            self._bus, self._redis_url, self._agent_id,
        )
        await self._redis_source.start()

        # 3. 启动 SubAgent 实时报告监听 (Redis Pub/Sub)
        self._report_listener = SubAgentReportListener(
            self._bus, self._redis_url,
        )
        await self._report_listener.start()

        # ── 将 report_listener 绑定到 PlanGraph (延迟绑定) ──
        if self._plan_graph and hasattr(self._plan_graph, 'set_report_listener'):
            self._plan_graph.set_report_listener(self._report_listener)
            logger.info("Report listener bound to PlanGraph")

        # 4. 启动 Timer 事件源 (注册系统定时器)
        self._timer_source = TimerEventSource(self._bus)
        await self._timer_source.start()

        # 注册系统定时器
        self._timer_source.register(TimerDef(
            name="health_check",
            interval_seconds=1200,  # 20 min
            timer_type="health_check",
        ))
        self._timer_source.register(TimerDef(
            name="cleanup_logs",
            interval_seconds=86400,  # 1 day
            timer_type="cleanup",
        ))

        # 5. 启动 WebSocket 源 (如有注入)
        if self._ws_source is not None:
            logger.info("WebSocket source active")

        logger.info("AgentRunLoop started — consuming events")

        # 进入主循环
        await self._run_loop()

    async def stop(self):
        """优雅关闭。"""
        logger.info("AgentRunLoop stopping...")
        self._running = False
        self._shutdown_event.set()

        # 停止事件源
        if self._redis_source:
            await self._redis_source.stop()
        if self._report_listener:
            await self._report_listener.stop()
        if self._timer_source:
            await self._timer_source.stop()

        # 停止 EventBus
        await self._bus.stop()

        logger.info("AgentRunLoop stopped")

    async def _run_loop(self):
        """主循环 — 消费 PriorityQueue 中的事件。

        设计原则 (对齐 iOS RunLoop):
          - 所有事件源 → 同一个 PriorityQueue
          - 纯阻塞 pop() — queue 空时 asyncio 挂起，零 CPU
          - Timer/Redis/WS 任何源有数据 → push() → queue 有数据 → pop() 自然唤醒
          - 没有事件时 100% 休眠，不轮询
          - shutdown → stop() → bus.push(SYSTEM_SHUTDOWN) → pop() 唤醒 → 退出

        优先级保证:
          prio=0 (用户消息) > prio=1 (celery_done) > prio=2 (progress) > prio=3 (timer)
          低 prio 值优先出队，用户消息永不被大量 progress 事件饿死。
        """
        from .event_bus import (
            EventType, UserMessageEvent, UserClarificationEvent, UserApprovalEvent,
            CeleryDoneEvent, CeleryErrorEvent, CeleryProgressEvent, TimerFiredEvent,
        )

        # 启动 WS 消息转发协程 (后台持续运行)
        if self._ws_source is not None:
            asyncio.create_task(self._forward_ws_messages())

        while self._running:
            try:
                # 纯阻塞等待 — 无事件时 asyncio 自动挂起协程
                # 任何源 push() 后自然唤醒
                priority, seq, event = await self._bus.pop()

                event_type = event.type
                if priority <= 1:  # prio=0/1 打 INFO，prio=2/3 打 DEBUG (降噪)
                    logger.info(f"[RunLoop] prio={priority} seq={seq} type={event_type}")
                else:
                    logger.debug(f"[RunLoop] prio={priority} seq={seq} type={event_type}")

                # ── 分发事件 ──────────────────────────────

                if event_type == EventType.SYSTEM_SHUTDOWN:
                    logger.info("Shutdown event received")
                    break

                elif event_type == EventType.USER_MESSAGE:
                    await self._handle_user_message(event)

                elif event_type == EventType.USER_CLARIFICATION:
                    await self._handle_clarification(event)

                elif event_type == EventType.USER_APPROVAL:
                    await self._handle_approval(event)

                elif event_type == EventType.CELERY_DONE:
                    await self._handle_celery_done(event)

                elif event_type == EventType.CELERY_ERROR:
                    await self._handle_celery_error(event)

                elif event_type == EventType.CELERY_PROGRESS:
                    await self._handle_celery_progress(event)

                elif event_type == EventType.TIMER_FIRED:
                    await self._handle_timer_fired(event)

                else:
                    logger.debug(f"Unknown event type: {event_type}")

                # 推送给订阅者
                await self._bus.publish_to_subscribers(event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AgentRunLoop dispatch error: {e}", exc_info=True)

    # ── WS 消息转发协程 ─────────────────────────────────

    async def _forward_ws_messages(self):
        """将 WebSocket 消息队列中的消息转发到 EventBus。

        后台协程 — 阻塞等待 WS 消息，有消息就推到 EventBus prio=0。
        WS 断开时 ws_source 会被设置为 None，协程自然退出。
        """
        from .event_bus import UserMessageEvent, UserClarificationEvent, UserApprovalEvent

        while self._running and self._ws_source is not None:
            try:
                ws_msg = await self._ws_source.get()  # 纯阻塞，有消息才醒
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.1)
                continue

            sub_type = ws_msg.get("subType", "")
            payload = ws_msg.get("payload", {})
            agent_id = ws_msg.get("agentId", self._agent_id)
            session_id = ws_msg.get("sessionId", "main")

            if sub_type == "chat":
                await self._bus.push(UserMessageEvent(
                    content=payload.get("text", ""),
                    raw_envelope=ws_msg,
                    agent_id=agent_id,
                    session_id=session_id,
                ))
            elif sub_type == "clarify":
                await self._bus.push(UserClarificationEvent(
                    answers=payload.get("answers", []),
                    agent_id=agent_id,
                    session_id=session_id,
                ))
            elif sub_type == "approve":
                await self._bus.push(UserApprovalEvent(
                    confirmed=payload.get("confirmed", False),
                    modifications=payload.get("modifications", {}),
                    agent_id=agent_id,
                    session_id=session_id,
                ))

    # ── 事件处理器 ──────────────────────────────────────

    async def _handle_user_message(self, event):
        """处理用户消息 → 启动/恢复 PlanGraph。"""
        session_id = getattr(event, 'session_id', 'main')
        config = {
            "configurable": {
                "thread_id": f"{self._agent_id}-plan-{session_id}",
            },
        }

        try:
            # 启动 PlanGraph 处理
            result = await self._graph.ainvoke(
                {"messages": [{"role": "user", "content": event.content}]},
                config=config,
            )
            await self._dispatch_graph_output(result, session_id)
        except Exception as e:
            logger.error(f"PlanGraph invocation failed: {e}", exc_info=True)

    async def _handle_clarification(self, event):
        """处理用户澄清回答 → aresume PlanGraph。"""
        session_id = getattr(event, 'session_id', 'main')
        config = {
            "configurable": {
                "thread_id": f"{self._agent_id}-plan-{session_id}",
            },
        }

        try:
            result = await self._graph.aresume(
                config,
                {"user_clarification": {"answers": event.answers}},
            )
            await self._dispatch_graph_output(result, session_id)
        except Exception as e:
            logger.error(f"PlanGraph aresume (clarify) failed: {e}", exc_info=True)

    async def _handle_approval(self, event):
        """处理用户审批 → aresume PlanGraph。"""
        session_id = getattr(event, 'session_id', 'main')
        config = {
            "configurable": {
                "thread_id": f"{self._agent_id}-plan-{session_id}",
            },
        }

        try:
            result = await self._graph.aresume(
                config,
                {"user_approval": {
                    "confirmed": event.confirmed,
                    "modifications": event.modifications,
                }},
            )
            await self._dispatch_graph_output(result, session_id)
        except Exception as e:
            logger.error(f"PlanGraph aresume (approval) failed: {e}", exc_info=True)

    async def _handle_celery_done(self, event: CeleryDoneEvent):
        """处理 Celery 任务完成。"""
        logger.info(f"Celery done: task={event.agent_task_id} agent={event.agent_type}")
        # 通过 WS 推送完成通知
        if self._ws_send_fn:
            await self._ws_send_fn({
                "role": "assistant",
                "type": "task",
                "subType": "done",
                "agentId": self._agent_id,
                "sessionId": getattr(event, 'session_id', 'main'),
                "seq": event.seq,
                "priority": event.priority,
                "timestamp": _now(),
                "payload": {
                    "taskId": event.agent_task_id,
                    "result": event.result,
                },
            })

    async def _handle_celery_error(self, event: CeleryErrorEvent):
        """处理 Celery 任务失败。"""
        logger.error(f"Celery error: task={event.agent_task_id} error={event.error[:200]}")
        if self._ws_send_fn:
            await self._ws_send_fn({
                "role": "assistant",
                "type": "task",
                "subType": "failed",
                "agentId": self._agent_id,
                "sessionId": getattr(event, 'session_id', 'main'),
                "seq": event.seq,
                "priority": event.priority,
                "timestamp": _now(),
                "payload": {
                    "taskId": event.agent_task_id,
                    "error": event.error,
                },
            })

    async def _handle_celery_progress(self, event: CeleryProgressEvent):
        """处理 Celery 进度 → WS 推送。"""
        if self._ws_send_fn:
            await self._ws_send_fn({
                "role": "assistant",
                "type": "task",
                "subType": "running",
                "agentId": self._agent_id,
                "sessionId": getattr(event, 'session_id', 'main'),
                "seq": event.seq,
                "priority": event.priority,
                "timestamp": _now(),
                "payload": {
                    "taskId": event.agent_task_id,
                    "stage": event.stage,
                    "stageIndex": event.stage_index,
                    "totalStages": event.total_stages,
                    "current": event.current,
                    "total": event.total,
                },
            })

    async def _handle_timer_fired(self, event: TimerFiredEvent):
        """处理定时触发事件。"""
        logger.info(f"Timer fired: {event.timer_name} (type={event.timer_type})")

        if event.timer_type == "health_check":
            await self._run_health_check()
        elif event.timer_type == "cleanup":
            await self._run_cleanup()
        elif event.timer_type == "subscription":
            # TODO: 检查订阅方向的新论文
            pass

    async def _run_health_check(self):
        """执行系统健康检查。"""
        ok = True
        status = {}

        # 检查 DB
        try:
            self._db.conn.execute("SELECT 1")
            status["db"] = "ok"
        except Exception as e:
            status["db"] = f"error: {e}"
            ok = False

        # 检查 Redis
        if self._redis_source:
            try:
                self._redis_source.redis.ping()
                status["redis"] = "ok"
            except Exception as e:
                status["redis"] = f"error: {e}"
                ok = False

        logger.info(f"Health check: {'OK' if ok else 'FAILED'} — {json.dumps(status)}")

    async def _run_cleanup(self):
        """清理过期日志。"""
        log_dir = Path.home() / ".paper_search" / "logs" / "tasks"
        if log_dir.exists():
            cutoff = datetime.now(timezone.utc).timestamp() - 30 * 86400  # 30 days
            count = 0
            for f in log_dir.glob("*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    count += 1
            if count:
                logger.info(f"Cleanup: removed {count} old task logs")

    # ── PlanGraph 输出分发 ──────────────────────────────

    async def _dispatch_graph_output(self, result, session_id: str):
        """将 PlanGraph 输出转为 WS 协议消息并发送。

        PlanGraph 节点可能设置:
          - pending_review → 转为 review(clarify/plan/permissions) WS 消息
          - plan_status → 转为 phase 消息
          - error → 转为 error 消息
        """
        if result is None:
            return

        # 检查 pending_review
        pending_review = result.get("pending_review")
        if pending_review and isinstance(pending_review, dict):
            await self._send_ws({
                "role": "assistant",
                "type": pending_review.get("type", "review"),
                "subType": pending_review.get("subType", "clarify"),
                "agentId": self._agent_id,
                "sessionId": session_id,
                "seq": 0,
                "priority": 0,
                "timestamp": _now(),
                "payload": pending_review.get("payload", {}),
            })

        # 检查错误
        error = result.get("error")
        if error:
            await self._send_ws({
                "role": "assistant",
                "type": "error",
                "subType": "error",
                "agentId": self._agent_id,
                "sessionId": session_id,
                "seq": 0,
                "priority": 0,
                "timestamp": _now(),
                "payload": {"message": str(error)},
            })

        # 检查 plan_status 变更
        plan_status = result.get("plan_status", "")
        if plan_status:
            await self._send_ws({
                "role": "assistant",
                "type": "phase",
                "subType": plan_status,
                "agentId": self._agent_id,
                "sessionId": session_id,
                "seq": 0,
                "priority": 0,
                "timestamp": _now(),
                "payload": {"planStatus": plan_status},
            })

    async def _send_ws(self, envelope: dict):
        """发送 WS 消息。"""
        if self._ws_send_fn:
            try:
                await self._ws_send_fn(envelope)
            except Exception as e:
                logger.error(f"WS send failed: {e}")


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


async def main(data_dir: Optional[str] = None, redis_url: Optional[str] = None):
    """daemon 主入口 — bootstrap + AgentRunLoop。

    启动流程:
      1. AgentBootstrap.bootstrap() → 创建/恢复 Agent 组件
      2. 创建 EventBus + AgentRunLoop
      3. 启动 RunLoop (阻塞直到 SIGTERM/SIGINT)
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

    # 创建 EventBus
    from .event_bus import EventBus
    bus = EventBus()

    # 创建 AgentRunLoop
    loop = AgentRunLoop(
        graph=result["graph"],
        bus=bus,
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
