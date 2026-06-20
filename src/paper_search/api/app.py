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
    import asyncio

    logger.info("Paper Agent API starting...")

    # 启动订阅通知监听器 (Redis Pub/Sub → WebSocket 桥接)
    try:
        from .ws import start_notification_listener
        asyncio.create_task(start_notification_listener())
        logger.info("Subscription notification listener started")
    except Exception as e:
        logger.warning(f"Notification listener not started: {e}")

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
    import asyncio as _asyncio
    from datetime import datetime, timezone

    await websocket.accept()
    await ws_manager.connect(agent_id, session_id, websocket)
    logger.info(f"WS connected: agent={agent_id}, session={session_id}")

    # ── Redis 连接 ──────────────────────────────────────
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
    except Exception as e:
        logger.warning(f"Redis unavailable for WS relay: {e}")
        _redis = None

    ws_queue = f"agent:ws:{agent_id}"
    output_channel = f"agent:output:{agent_id}:{session_id}"
    handshake_done = False

    # ── 后台 Task: Redis Pub/Sub → WebSocket ───────────
    async def _output_relay():
        if not _redis:
            return
        try:
            pubsub = _redis.pubsub()
            await pubsub.subscribe(output_channel)
            logger.debug(f"Output relay started: {output_channel}")
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data = _json.loads(msg["data"])
                    text = _json.dumps(data, ensure_ascii=False, default=str)
                    await websocket.send_text(text)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Output relay ended: {e}")

    relay_task = _asyncio.create_task(_output_relay()) if _redis else None

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

    async def _send_json(data: dict):
        """发送 JSON 到 WebSocket（不含持久化，daemon 负责记录）。"""
        text = _json.dumps(data, ensure_ascii=False, default=str)
        await websocket.send_text(text)

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

            # ── 握手（v8.0: 本地处理，不跑 PlanGraph）────
            if not handshake_done and msg_type == "message" and msg_sub == "chat":
                handshake_done = True
                db = get_db()
                existing = db.get_session(agent_id, session_id)
                if not existing:
                    db.create_session(agent_id, session_id, title="新对话")
                history_count = db.get_history_count(agent_id, session_id)
                active_tasks = db.get_active_tasks(agent_id, session_id)

                await _send_json(_envelope(
                    type="phase", subType="connected", priority=0, seq=0,
                    payload={
                        "sessionTitle": (existing or {}).get("title", "新对话") if existing else "新对话",
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
                logger.info(f"Handshake: agent={agent_id}, session={session_id}, history={history_count}")

            if not handshake_done:
                await _send_and_store(_envelope(
                    type="error", subType="INTERNAL_ERROR",
                    payload={
                        "message": "Handshake required — first message must be message(chat) with seq=1",
                        "recoverable": False,
                    },
                ))
                continue

            # ── 消息分发 (v8.0: LPUSH → Redis → Daemon) ──
            try:
                if msg_type == "heartbeat" and msg_sub == "ping":
                    await _send_and_store(_envelope(
                        type="heartbeat", subType="pong", priority=0, seq=0, payload={},
                    ))

                else:
                    # 所有业务消息 → LPUSH Redis → Daemon 处理
                    msg["_session_id"] = session_id
                    msg["_agent_id"] = agent_id
                    _redis.lpush(ws_queue, _json.dumps(msg, ensure_ascii=False, default=str))
                    logger.debug(f"WS → Redis: type={msg_type}, subType={msg_sub}")

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
        if relay_task:
            relay_task.cancel()
            try:
                await relay_task
            except _asyncio.CancelledError:
                pass
        if _redis:
            await _redis.close()
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
