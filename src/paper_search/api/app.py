"""FastAPI 应用 — Paper Agent REST + WebSocket.

启动方式:
    uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000
    或
    python -m paper_search.api.app
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import router
from .ws import ws_manager

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Lifespan — 惰性初始化
# ═══════════════════════════════════════════════════════════════


_db = None
_engine = None
_llm = None
_chroma = None
_kb = None


def get_db():
    global _db
    if _db is None:
        from ..agent.db import AgentDB
        _db = AgentDB()
    return _db


def get_engine():
    global _engine
    if _engine is None:
        from ..config import Config
        from ..engine import PaperSearchEngine
        _load_providers()
        _engine = PaperSearchEngine(Config())
    return _engine


def get_llm():
    global _llm
    if _llm is None:
        from ..agent.llm_client_v2 import LLMClientV2
        _llm = LLMClientV2()
    return _llm


def get_chroma():
    global _chroma
    if _chroma is None:
        from ..agent.chroma_store import ChromaStoreV2
        _chroma = ChromaStoreV2()
    return _chroma


def get_kb():
    global _kb
    if _kb is None:
        from ..agent.knowledge import KnowledgeBase
        _kb = KnowledgeBase(get_db(), get_chroma(), get_llm())
    return _kb


def _load_providers():
    for mod in ["arxiv_provider", "semanticscholar_provider", "pubmed_provider",
                "cnki_provider", "ieee_provider", "sciencedirect_provider"]:
        try:
            __import__(f"..providers.{mod}", fromlist=["paper_search.providers"],
                      globals=globals(), level=1)
        except ImportError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期 — 启动时初始化，关闭时清理."""
    logger.info("Paper Agent API starting...")
    yield
    logger.info("Paper Agent API shutting down...")
    if _db:
        _db.close()
    if _engine:
        await _engine.close()


# ═══════════════════════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Paper Agent API",
    description="学术论文搜索与科研助理 API — 多源搜索、自动下载、知识库管理",
    version="3.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(router)


# ═══════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws/chat/{agent_id}/{session_id}")
async def ws_chat(websocket: WebSocket, agent_id: str, session_id: str):
    """WebSocket 对话通道 — 完整协议 v7.0 实现.

    协议见 docs/development/websocket-protocol.md
    """
    import json as _json
    from datetime import datetime, timezone
    from .message_store import MessageStore

    await websocket.accept()
    await ws_manager.connect(agent_id, session_id, websocket)
    logger.info(f"WS connected: agent={agent_id}, session={session_id}")

    handshake_done = False
    store = MessageStore(get_db())
    _pending_graph = None
    _graph_config = {"configurable": {"thread_id": f"{agent_id}-{session_id}"}}

    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _envelope(**overrides) -> dict:
        """构建符合协议的信封。默认 role=assistant。"""
        defaults = {
            "role": "assistant", "type": "error", "subType": "INTERNAL_ERROR",
            "agentId": agent_id, "sessionId": session_id,
            "seq": 0, "priority": 2, "timestamp": _now(), "payload": {},
        }
        return defaults | overrides

    def _maybe_create_session() -> tuple[bool, dict | None]:
        """检查或创建 session。返回 (是否已存在, session_dict)。"""
        db = get_db()
        existing = db.get_session(agent_id, session_id)
        if existing:
            return True, existing
        db.create_session(agent_id, session_id, title="新对话")
        logger.info(f"Auto-created session: agent={agent_id}, session={session_id}")
        new_session = db.get_session(agent_id, session_id)
        return False, new_session

    async def _send_and_store(envelope: dict):
        """发送 WS 消息 + 持久化到 DB。"""
        text = _json.dumps(envelope, ensure_ascii=False, default=str)
        await websocket.send_text(text)
        await store.save_envelope(envelope)

    # ── TaskEventAdapter ────────────────────────────────────
    from ..agent.task_event_adapter import TaskEventAdapter

    async def _task_send_fn(envelope: dict):
        """TaskEventAdapter 的回调：发送 + 持久化 task/message(notification)。"""
        await _send_and_store(envelope)

    task_adapter = TaskEventAdapter(
        agent_id=agent_id, session_id=session_id,
        send_fn=_task_send_fn,
    )

    async def _process_graph_stream(graph_input):
        """处理 Plan Graph astream_events() 的事件流。

        对初始输入和 resume(Command) 统一使用此函数。
        """
        nonlocal _pending_graph

        graph = await _get_plan_graph(task_adapter=task_adapter)

        try:
            async for event in graph.astream_events(
                graph_input, config=_graph_config, version="v2",
            ):
                kind = event.get("event", "")
                name = event.get("name", "")
                data = event.get("data", {})

                if kind == "on_chat_model_stream":
                    chunk = data.get("chunk", {})
                    if hasattr(chunk, "content") and chunk.content:
                        await _send_and_store(_envelope(
                            type="message", subType="text", priority=0,
                            seq=0,
                            payload={"index": 0, "delta": str(chunk.content), "done": False},
                        ))

                elif kind == "on_chain_start" and name == "clarify":
                    await _send_and_store(_envelope(
                        type="phase", subType="clarify", priority=0, seq=0,
                        payload={"stage": "分析需求", "message": "正在分析你的研究需求..."},
                    ))

                elif kind == "on_chain_end":
                    output = data.get("output", {}) or {}
                    pending_review = output.get("pending_review") if isinstance(output, dict) else None

                    if name == "clarify" and pending_review:
                        # 发送 review(clarify) 给客户端
                        review_env = _envelope(
                            type="review", subType="clarify", priority=2, seq=0,
                            payload=pending_review.get("payload", {}),
                        )
                        await _send_and_store(review_env)
                        _pending_graph = graph  # 等待 resume

                    elif name == "generate_plan" and pending_review:
                        plan_payload = pending_review.get("payload", {})
                        await _send_and_store(_envelope(
                            type="review", subType="plan", priority=2, seq=0,
                            payload=plan_payload,
                        ))
                        _pending_graph = graph

                    elif name == "await_permissions" and pending_review:
                        # 发送 review(permissions) — 权限确认（当前自动授予）
                        perm_payload = pending_review.get("payload", {})
                        await _send_and_store(_envelope(
                            type="review", subType="permissions", priority=2, seq=0,
                            payload=perm_payload,
                        ))
                        # 自动批准所有权限
                        from langgraph.types import Command
                        await _process_graph_stream(
                            Command(resume={"permissions_granted": True})
                        )

                    elif name == "execute_plan":
                        await _send_and_store(_envelope(
                            type="phase", subType="execute", priority=0, seq=0,
                            payload={"stage": "执行方案", "message": "正在执行研究方案..."},
                        ))

                    elif name == "overall_evaluate":
                        output = output if isinstance(output, dict) else {}
                        assessment = output.get("evaluate_assessment", "satisfied")
                        if assessment == "adjust":
                            await _send_and_store(_envelope(
                                type="phase", subType="verify", priority=0, seq=0,
                                payload={"stage": "验证", "message": "结果不足，需要调整策略"},
                            ))
                        else:
                            plan = output.get("plan", {}) or {}
                            results = plan.get("execution_results", [])
                            reply_content = f"## 研究完成\n\n已完成 {len(results)} 个子任务。"
                            await _send_and_store(_envelope(
                                type="message", subType="reply", priority=1, seq=0,
                                payload={"content": reply_content},
                            ))
                            await _send_and_store(_envelope(
                                type="phase", subType="done", priority=0, seq=0,
                                payload={"stage": "完成", "message": "全部任务完成"},
                            ))

                elif kind == "on_tool_start":
                    tool_name = data.get("name", event.get("name", ""))
                    if tool_name:
                        await _send_and_store(_envelope(
                            type="tool", subType="server", priority=0, seq=0,
                            payload={"id": event.get("run_id", ""), "name": str(tool_name),
                                     "input": {}, "status": "running"},
                        ))

                elif kind == "on_tool_end":
                    tool_name = data.get("name", event.get("name", ""))
                    if tool_name:
                        await _send_and_store(_envelope(
                            type="tool", subType="server", priority=0, seq=0,
                            payload={"id": event.get("run_id", ""), "name": str(tool_name),
                                     "input": {}, "status": "done"},
                        ))

        except Exception as e:
            logger.error(f"Plan Graph execution error: {e}", exc_info=True)
            await _send_and_store(_envelope(
                type="error", subType="TASK_FAILED", priority=2, seq=0,
                payload={"message": f"Execution error: {e}", "recoverable": True},
            ))

    async def _handle_chat_message(msg: dict):
        """处理用户 chat 消息 → Plan Graph astream_events()。"""
        content = (msg.get("payload") or {}).get("content", "")
        seq = msg.get("seq", 0)

        # ── 前台→后台 切换（v7.0: task 类型）────
        # 用户发新消息 → 当前前台任务转入后台（不可逆）
        foreground_task = get_db().get_foreground_task(session_id)
        if foreground_task:
            ft_id = foreground_task.get("id", "")
            try:
                get_db().set_task_mode(ft_id, "background")
                get_db().update_agent_task(ft_id, status="running")
            except Exception:
                pass
            await task_adapter.on_task_backgrounded(ft_id, reason="user_new_message")

        # 保存用户消息
        await store.save_user_message(agent_id, session_id, seq, msg.get("payload", {}))

        # 构建输入
        user_input = {
            "messages": [{"role": "user", "content": content}],
            "session_id": session_id,
            "agent_id": agent_id,
        }

        await _process_graph_stream(user_input)

    async def _handle_review_response(msg: dict):
        """处理用户 review 回复 → graph ainvoke(Command(resume=...))."""
        from langgraph.types import Command

        msg_sub = msg.get("subType", "")
        payload = msg.get("payload", {})

        if msg_sub == "clarify":
            answers = payload.get("answers", [])
            await _process_graph_stream(
                Command(resume={"user_clarification": {"answers": answers}})
            )
        elif msg_sub == "plan":
            confirmed = payload.get("confirmed", False)
            if confirmed:
                task_id = payload.get("taskId", "")
                # v7.0: 方案批准 → 立即发送 task(started, mode=foreground)
                await task_adapter.on_task_started(
                    task_id=task_id,
                    name=payload.get("goal", "论文调研"),
                    mode="foreground",
                    total_stages=len(payload.get("steps", [])) or 7,
                )
                await _process_graph_stream(
                    Command(resume={"user_approval": {"confirmed": True, "modifications": payload.get("modifications", {})}})
                )
            else:
                await _send_and_store(_envelope(
                    type="message", subType="plan_rejected", priority=2, seq=0,
                    payload={"taskId": payload.get("taskId", ""), "reason": payload.get("reason", "User rejected")},
                ))

    async def _handle_tool_result(msg: dict):
        """处理 iOS tool(result) → graph ainvoke(Command(resume=...))."""
        from langgraph.types import Command

        payload = msg.get("payload", {})
        tool_call_id = payload.get("tool_call_id", "")
        content = payload.get("content", "")
        error = payload.get("error", "")
        await _process_graph_stream(
            Command(resume={"tool_result": {"tool_call_id": tool_call_id, "content": content, "error": error}})
        )

    async def _handle_task_control(msg: dict):
        """处理 task_control → 暂停/恢复/取消。"""
        payload = msg.get("payload", {})
        action = payload.get("action", "pause")
        task_id = payload.get("taskId", "")
        db = get_db()
        status_map = {"pause": "paused", "resume": "running", "cancel": "cancelled"}
        new_status = status_map.get(action, "paused")
        if task_id:
            try:
                db.update_agent_task(task_id, status=new_status)
            except Exception:
                pass
        await _send_and_store(_envelope(
            type="phase", subType="paused" if action == "pause" else "execute",
            priority=0, seq=0,
            payload={"taskId": task_id, "action": action},
        ))

    # ── 主消息循环 ────────────────────────────────────────

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = _json.loads(raw)
            except _json.JSONDecodeError:
                await _send_and_store(_envelope(
                    type="error", subType="INTERNAL_ERROR",
                    payload={"message": "Invalid JSON", "recoverable": True},
                ))
                continue

            msg_type = msg.get("type", "")
            msg_sub = msg.get("subType", "")
            msg_seq = msg.get("seq", 0)

            # ── 握手 ──────────────────────────────────────
            if not handshake_done and msg_type == "message" and msg_sub == "chat":
                handshake_done = True
                existed, session_data = _maybe_create_session()

                # 加载重连回放消息
                replay_msgs = await store.get_replay_messages(agent_id, session_id) if existed else []
                history_count = get_db().get_history_count(agent_id, session_id) if existed else 0
                active_tasks = get_db().get_active_tasks(agent_id, session_id) if existed else []
                session_title = (session_data or {}).get("title", None)

                # 发送 phase(connected) — v7.0 格式含完整任务卡片信息
                await _send_and_store(_envelope(
                    type="phase", subType="connected", priority=0, seq=0,
                    payload={
                        "sessionTitle": session_title,
                        "historyCount": history_count,
                        "activeTasks": [
                            {
                                "taskId": t.get("taskId", t.get("id", "")),
                                "name": t.get("name", "未命名任务"),
                                "mode": t.get("mode", "foreground"),
                                "stage": t.get("stage", t.get("status", "")),
                                "current": t.get("current", 0),
                                "total": t.get("total", 0),
                                "status": t.get("status", "pending"),
                            }
                            for t in active_tasks
                        ],
                    },
                ))
                logger.info(f"Handshake: agent={agent_id}, session={session_id}, existed={existed}, history={history_count}")

                # 回放过期 review 和 error
                for rmsg in replay_msgs:
                    await ws_manager.broadcast(agent_id, session_id, rmsg)

                # 处理首条 chat 消息
                await _handle_chat_message(msg)
                continue

            if not handshake_done:
                await _send_and_store(_envelope(
                    type="error", subType="INTERNAL_ERROR",
                    payload={
                        "message": "Handshake required — first message must be message(chat) with seq=1",
                        "recoverable": False,
                    },
                ))
                continue

            # ── 消息分发 ───────────────────────────────────
            try:
                key = (msg_type, msg_sub)

                if msg_type == "heartbeat" and msg_sub == "ping":
                    await _send_and_store(_envelope(
                        type="heartbeat", subType="pong", priority=0, seq=0, payload={},
                    ))

                elif key == ("message", "chat"):
                    await _handle_chat_message(msg)

                elif key == ("review", "clarify"):
                    await store.save_envelope(msg)
                    await _handle_review_response(msg)

                elif key == ("review", "plan"):
                    await store.save_envelope(msg)
                    await _handle_review_response(msg)

                elif key == ("review", "task_control"):
                    await _handle_task_control(msg)

                elif key == ("tool", "result"):
                    await store.save_envelope(msg)
                    await _handle_tool_result(msg)

                else:
                    logger.debug(f"Unhandled WS message: type={msg_type}, subType={msg_sub}")
            except Exception as handler_err:
                logger.error(f"Message handler error: {handler_err}", exc_info=True)
                await _send_and_store(_envelope(
                    type="error", subType="INTERNAL_ERROR", priority=2, seq=0,
                    payload={"message": f"Handler error: {handler_err}", "recoverable": True},
                ))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: agent={agent_id}, session={session_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        await ws_manager.disconnect(agent_id, session_id, websocket)


# ── Plan Graph 惰性初始化 ──────────────────────────────

_graph = None
_saver = None
_aiosqlite_conn = None

async def _get_plan_graph(task_adapter=None):
    """惰性初始化 LangGraph Plan Graph（异步，需要 aiosqlite 连接）。

    Args:
        task_adapter: TaskEventAdapter 实例（用于 task WS 消息推送）
    """
    global _graph, _saver, _aiosqlite_conn
    if _graph is None:
        import aiosqlite
        from ..agent.graphs.plan_graph import PlanGraph
        from ..agent.tool_registry import ToolRegistry
        from ..agent.memory import MemoryManager
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        llm = get_llm()
        db = get_db()
        chroma = get_chroma()
        tools = ToolRegistry.get_instance()
        memory = MemoryManager(db, chroma)
        pg = PlanGraph(llm=llm, tools=tools, memory=memory, db=db,
                       task_adapter=task_adapter)
        _aiosqlite_conn = await aiosqlite.connect(str(db.db_path))
        await _aiosqlite_conn.execute("PRAGMA journal_mode=WAL")
        await _aiosqlite_conn.execute("PRAGMA busy_timeout=30000")
        _saver = AsyncSqliteSaver(conn=_aiosqlite_conn)
        await _saver.setup()
        _graph = pg.compile(checkpointer=_saver)
        logger.info("Plan Graph lazily initialized")
    return _graph


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    import uvicorn
    uvicorn.run("paper_search.api.app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
