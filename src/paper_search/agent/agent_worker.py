"""Agent Worker — 子进程入口。

v4.2: 启动上报 startup_ms / graph_nodes，Supervisor 据此判断就绪。

v4.1: Agent 不直接连接 Redis。所有 IO 通过 stdin/stdout pipe 与 Supervisor 通信。

通信协议 (JSON lines on stdout):
  {"type":"state","state":"busy","node":"intent_classify","active_turns":1}
  {"type":"reply","content":"综述生成完毕...","session_id":"main"}
  {"type":"status","stage":"searching","message":"找到 64 篇论文"}
  {"type":"error","subType":"TASK_FAILED","message":"搜索超时"}

stdin: 每行一个 JSON 消息 (由 Supervisor 写入)
stderr: 崩溃 traceback → Supervisor 日志

用法:
    python -m paper_search.agent.agent_worker --user-id user-abc
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import select
import signal
import sys
import time as _time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def supervisor_send(msg_type: str, **payload):
    """Send a message to Supervisor via stdout."""
    msg = {"type": msg_type, "timestamp": _now_iso()}
    msg.update(payload)
    print(json.dumps(msg, ensure_ascii=False), flush=True)


class AgentWorker:
    """Agent 子进程: stdin → process → stdout。"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.agent_id = f"agent-{user_id}"
        self._agent: Any = None
        self._db: Any = None
        self._active_turns: int = 0
        self._current_session_id: str = ""

    async def bootstrap(self) -> bool:
        """初始化 DB / LLM / Tools / Memory / LangGraph。"""
        t0 = _time.time()
        logger.info("Worker %s: bootstrap starting", self.user_id)
        try:
            # DB
            logger.info("Worker %s: connecting to DB...", self.user_id)
            from ..agent.pgdb import PostgresAgentDB
            self._db = PostgresAgentDB()
            logger.info("Worker %s: DB connected", self.user_id)

            # LLM
            logger.info("Worker %s: loading LLM client...", self.user_id)
            from ..agent.llm_client_v2 import get_llm_client
            self._llm = get_llm_client()
            logger.info("Worker %s: LLM client ready", self.user_id)

            # Tools
            logger.info("Worker %s: loading tools...", self.user_id)
            from ..agent.tool_registry import ToolRegistry, set_db
            set_db(self._db, user_id=self.user_id)
            self._tools = ToolRegistry.get_instance()
            logger.info("Worker %s: %d tools loaded", self.user_id,
                         len(self._tools.tool_names))

            # Build MainGraph — Agent 不连 Redis，push_fn 走 stdout
            logger.info("Worker %s: compiling graph...", self.user_id)
            from .graphs.main_graph import build_main_graph

            async def _graph_push(session_id, msg_type, subtype, role,
                                   payload=None, priority_kind="normal"):
                supervisor_send(msg_type, session_id=session_id,
                                 **(payload or {}))

            self._compiled_graph = build_main_graph(
                llm=self._llm, registry=self._tools, db=self._db,
                push_fn=_graph_push,
            )
            logger.info("Worker %s: graph compiled", self.user_id)

            elapsed_ms = int((_time.time() - t0) * 1000)
            graph_nodes = list(self._compiled_graph.nodes.keys()) if hasattr(self._compiled_graph, 'nodes') else []

            logger.info("Worker %s: bootstrap complete (elapsed=%dms nodes=%d)",
                       self.user_id, elapsed_ms, len(graph_nodes))
            supervisor_send("state", state="idle", node=None,
                             active_turns=0, agent_id=self.agent_id,
                             startup_ms=elapsed_ms,
                             graph_nodes=graph_nodes)
            return True
        except Exception as e:
            logger.error("Bootstrap failed: %s", e, exc_info=True)
            supervisor_send("error", subType="INTERNAL_ERROR",
                             message=f"Bootstrap failed: {e}")
            return False

    async def process_message(self, raw_msg: str) -> str:
        """Process one user message → return reply content."""
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning("Worker %s: non-JSON stdin message (%d chars): %s",
                          self.user_id, len(raw_msg), raw_msg[:200])
            return ""

        session_id = msg.get("_session_id", "main")
        user_content = msg.get("payload", {}).get("content", "")
        if not user_content:
            logger.debug("Worker %s: empty user_content, falling back to raw message", self.user_id)
            user_content = raw_msg

        self._current_session_id = session_id
        self._active_turns += 1

        supervisor_send("state", state="busy", node="intent_classify",
                        active_turns=self._active_turns,
                        session_id=session_id)

        correlation_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": session_id}}

        result = None
        try:
            state = await self._compiled_graph.ainvoke({
                "user_content": user_content,
                "session_id": session_id,
            }, config=config)
            result = state.get("final_reply", "") or ""
        except asyncio.CancelledError:
            logger.info("Worker %s: graph invoke cancelled (session=%s)", self.user_id, session_id)
            self._active_turns = max(0, self._active_turns - 1)
            raise
        except Exception as e:
            logger.error("Graph invoke failed: %s", e, exc_info=True)
            supervisor_send("error", subType="TASK_FAILED",
                            message=str(e)[:200])
            result = f"抱歉，处理出错: {e}"
        finally:
            self._active_turns = max(0, self._active_turns - 1)

        supervisor_send("state", state="idle", node=None,
                        active_turns=self._active_turns,
                        session_id=session_id)
        return result

    async def run(self):
        """主循环: stdin.readline → process → stdout reply。支持 SIGTERM 优雅退出。"""
        logger.info("AgentWorker %s running (pid=%d)", self.user_id, os.getpid())

        loop = asyncio.get_event_loop()
        _stop_requested = False

        def _handle_signal(signum, frame):
            nonlocal _stop_requested
            _stop_requested = True
            logger.info("Worker %s: received signal %d, shutting down gracefully",
                        self.user_id, signum)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        while not _stop_requested:
            # select 实现可中断的非阻塞 stdin 检查（取代阻塞 readline）
            def _read_ready():
                return select.select([sys.stdin], [], [], 0.5)[0]
            try:
                ready = await loop.run_in_executor(None, _read_ready)
            except Exception as e:
                logger.warning("Worker %s: select error: %s", self.user_id, e)
                await asyncio.sleep(0.5)
                continue

            if not ready:
                continue

            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception as e:
                logger.warning("Worker %s: readline error: %s", self.user_id, e)
                await asyncio.sleep(0.5)
                continue

            if not line:
                logger.info("Worker %s: stdin closed, exiting", self.user_id)
                break

            line = line.strip()
            if not line:
                continue

            result = await self.process_message(line)
            if result:
                supervisor_send("reply", content=result,
                                session_id=self._current_session_id)

        supervisor_send("state", state="stopped", node=None, active_turns=0)
        logger.info("Worker %s: shutdown complete", self.user_id)


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════

async def main(user_id: str):
    logger.info("AgentWorker starting for user=%s", user_id)
    worker = AgentWorker(user_id)
    ok = await worker.bootstrap()
    if not ok:
        logger.error("Worker %s: bootstrap failed, exiting", user_id)
        supervisor_send("error", subType="INTERNAL_ERROR",
                        message="Bootstrap failed")
        return
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    user_id = None
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--user-id" and i + 1 < len(args):
            user_id = args[i + 1]
            break
        if arg.startswith("--user-id="):
            user_id = arg.split("=", 1)[1]
            break

    if not user_id:
        print(json.dumps({"type": "error", "subType": "INTERNAL_ERROR",
                          "message": "--user-id is required"}))
        sys.exit(1)

    asyncio.run(main(user_id))
