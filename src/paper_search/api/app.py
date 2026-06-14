"""FastAPI 应用 — Paper Agent REST + WebSocket + SSE.

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
_agent_loop = None
_auto_pipeline = None
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


def get_agent_loop():
    global _agent_loop
    if _agent_loop is None:
        from ..agent.agent_loop import AgentLoop
        from ..agent.memory import MemoryManager
        memory = MemoryManager(get_db(), get_chroma())
        _agent_loop = AgentLoop(get_db(), get_llm(), memory)
    return _agent_loop


def get_auto_pipeline():
    global _auto_pipeline
    if _auto_pipeline is None:
        from ..agent.auto_pipeline import AutoPipeline
        _auto_pipeline = AutoPipeline(get_db(), get_engine(), get_llm(), get_chroma())
    return _auto_pipeline


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

@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: str):
    """WebSocket 对话通道.

    消息格式 (JSON):
      Client → Server:
        {"type": "chat", "payload": {"message": "我想研究..."}}
        {"type": "clarify_response", "payload": {"question_id": "q1", "answer": "..."}}
        {"type": "plan_confirm", "payload": {"task_id": "...", "confirmed": true}}
        {"type": "task_control", "payload": {"task_id": "...", "action": "pause/resume/cancel"}}

      Server → Client:
        {"type": "chat_response", "payload": {"message": "..."}}
        {"type": "clarify_question", "payload": {"questions": [...]}}
        {"type": "plan", "payload": {"json": {...}, "markdown": "..."}}
        {"type": "progress", "payload": {"step": 3, "total": 8, "status": "executing"}}
        {"type": "step_result", "payload": {"step": {...}, "verification": {...}}}
        {"type": "error", "payload": {"message": "...", "recoverable": true}}
    """
    import json as _json

    await websocket.accept()
    loop = get_agent_loop()

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = _json.loads(raw)
            except _json.JSONDecodeError:
                await websocket.send_text(_json.dumps({
                    "type": "error",
                    "payload": {"message": "Invalid JSON", "recoverable": True},
                }, ensure_ascii=False))
                continue

            msg_type = msg.get("type", "")
            payload = msg.get("payload", {})

            if msg_type == "chat":
                user_msg = payload.get("message", "")
                if user_msg:
                    result = await loop.run_full_pipeline(
                        user_msg,
                        session_id=session_id,
                    )
                    await websocket.send_text(_json.dumps({
                        "type": result["stage"],
                        "payload": result,
                    }, ensure_ascii=False, default=str))

            elif msg_type == "clarify_response":
                # 用户回答了澄清问题，继续 plan
                answers = payload.get("answers", [])
                result = await loop.run_full_pipeline(
                    payload.get("original_query", ""),
                    session_id=session_id,
                    clarified_answers=answers,
                )
                await websocket.send_text(_json.dumps({
                    "type": result["stage"],
                    "payload": result,
                }, ensure_ascii=False, default=str))

            elif msg_type == "plan_confirm":
                task_id = payload.get("task_id", "")
                if payload.get("confirmed") and task_id:
                    # 异步执行
                    import asyncio as _asyncio

                    async def run_and_report():
                        try:
                            results = await loop.execute_plan(
                                task_id,
                                on_progress=lambda idx, total, status, name: (
                                    websocket.send_text(_json.dumps({
                                        "type": "progress",
                                        "payload": {"step": idx + 1, "total": total, "status": status, "name": name},
                                    }, ensure_ascii=False))
                                ),
                            )
                            await websocket.send_text(_json.dumps({
                                "type": "execution_complete",
                                "payload": results,
                            }, ensure_ascii=False, default=str))
                        except Exception as e:
                            await websocket.send_text(_json.dumps({
                                "type": "error",
                                "payload": {"message": str(e), "recoverable": False},
                            }, ensure_ascii=False))

                    _asyncio.create_task(run_and_report())
                    await websocket.send_text(_json.dumps({
                        "type": "execution_started",
                        "payload": {"task_id": task_id},
                    }, ensure_ascii=False))
                else:
                    await websocket.send_text(_json.dumps({
                        "type": "plan_rejected",
                        "payload": {"task_id": task_id, "message": "方案已拒绝"},
                    }, ensure_ascii=False))

            elif msg_type == "task_control":
                task_id = payload.get("task_id", "")
                action = payload.get("action", "")
                if action == "pause":
                    await loop.pause(task_id)
                elif action == "resume":
                    await loop.resume(task_id)
                elif action == "cancel":
                    await loop.cancel(task_id)
                await websocket.send_text(_json.dumps({
                    "type": "task_control_ack",
                    "payload": {"task_id": task_id, "action": action, "status": "ok"},
                }, ensure_ascii=False))

            else:
                await websocket.send_text(_json.dumps({
                    "type": "error",
                    "payload": {"message": f"Unknown message type: {msg_type}", "recoverable": True},
                }, ensure_ascii=False))

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: session={session_id}")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    import uvicorn
    uvicorn.run("paper_search.api.app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
