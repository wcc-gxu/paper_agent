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
    """WebSocket 对话通道 — 主 Agent Plan Graph 入口.

    协议见 docs/development/websocket-protocol.md

    握手流程:
      ① iOS → Server:  WS 连接 /ws/chat/{agent_id}/{session_id}
      ② iOS → Server:  message(chat, seq=1) — 首条消息夹带握手信息
      ③ Server:        检查 session → 不存在则自动创建
      ④ Server → iOS:  phase(connected) — 握手回复
      ⑤ 握手完成，进入正常交互

    [REWRITE PENDING] 消息路由待 LangGraph Plan Graph 接入。
    """
    import json as _json
    from datetime import datetime, timezone

    await websocket.accept()
    await ws_manager.connect(agent_id, session_id, websocket)
    logger.info(f"WS connected: agent={agent_id}, session={session_id}")

    # Mutable handshake state — True after first chat with seq=1 is acked
    handshake_done = False

    def _envelope(**overrides) -> dict:
        """Build a protocol-compliant message envelope with defaults."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "role": "assistant",
            "type": "error",
            "subType": "INTERNAL_ERROR",
            "agentId": agent_id,
            "sessionId": session_id,
            "seq": 0,
            "priority": 2,
            "timestamp": now,
            "payload": {},
        } | overrides

    def _maybe_create_session() -> bool:
        """Check session exists; auto-create if not.  Returns True = already existed."""
        db = get_db()
        existing = db.conn.execute(
            "SELECT 1 FROM sessions WHERE agent_id=? AND session_id=?",
            (agent_id, session_id),
        ).fetchone()
        if existing:
            return True
        db.conn.execute(
            "INSERT INTO sessions (agent_id, session_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, datetime('now'), datetime('now'))",
            (agent_id, session_id, "新对话"),
        )
        db.conn.commit()
        logger.info(f"Auto-created session: agent={agent_id}, session={session_id}")
        return False

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = _json.loads(raw)
            except _json.JSONDecodeError:
                await websocket.send_text(_json.dumps(
                    _envelope(
                        type="error", subType="INTERNAL_ERROR",
                        payload={"message": "Invalid JSON", "recoverable": True},
                    ), ensure_ascii=False,
                ))
                continue

            msg_type = msg.get("type", "")
            msg_sub = msg.get("subType", "")
            msg_seq = msg.get("seq", 0)

            # ── Handshake ──────────────────────────────────────────
            if not handshake_done and msg_type == "message" and msg_sub == "chat":
                handshake_done = True
                existed = _maybe_create_session()

                # Build connected payload
                payload: dict[str, object] = {
                    "sessionTitle": None if not existed else None,  # TODO: load from DB
                    "historyCount": 0,                              # TODO: real count
                    "activeTasks": [],                              # TODO: real tasks
                }
                await websocket.send_text(_json.dumps(
                    _envelope(
                        type="phase", subType="connected",
                        priority=0, payload=payload,
                    ), ensure_ascii=False,
                ))
                logger.info(f"Handshake complete: agent={agent_id}, session={session_id}, existed={existed}")

                # [REWRITE] 消息处理 → LangGraph Plan Graph.astream()
                continue

            if not handshake_done:
                # 首条消息必须是 message(chat)
                await websocket.send_text(_json.dumps(
                    _envelope(
                        type="error", subType="INTERNAL_ERROR",
                        payload={
                            "message": "Handshake required — first message must be message(chat) with seq=1",
                            "recoverable": False,
                        },
                    ), ensure_ascii=False,
                ))
                continue

            # ── Normal message processing ───────────────────────────
            # TODO: LangGraph Plan Graph dispatch by type/subType
            logger.debug(f"WS message: agent={agent_id}, session={session_id}, type={msg_type}, subType={msg_sub}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: agent={agent_id}, session={session_id}")
    finally:
        await ws_manager.disconnect(agent_id, session_id, websocket)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    import uvicorn
    uvicorn.run("paper_search.api.app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
