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
    """WebSocket 中继 — v9.0 无握手协议。

    - 连接即用，不需要握手
    - 收消息 → LPUSH Redis → Daemon 消费
    - Daemon 回复 → Pub/Sub → WS send → iOS
    - 永不主动断开连接
    """
    import json as _json
    import os
    import asyncio as _asyncio

    await websocket.accept()
    await ws_manager.connect(agent_id, session_id, websocket)
    logger.info(f"WS connected: agent={agent_id}, session={session_id}")

    # ── Redis 连接 (带重试) ────────────────────────────
    _redis = None
    for attempt in range(3):
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(
                os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
            )
            await _redis.ping()
            break
        except Exception as e:
            logger.warning(f"Redis connect attempt {attempt+1}/3: {e}")
            await _asyncio.sleep(1)
    if not _redis:
        await websocket.close(code=1011, reason="Redis unavailable")
        return

    ws_queue = f"agent:ws:{agent_id}"
    output_channel = f"agent:output:{agent_id}:{session_id}"

    # ── 后台 Task: Redis Pub/Sub → WebSocket ───────────
    async def _output_relay():
        """订阅 Daemon output channel，转发到 WebSocket。不断线重连。"""
        while True:
            try:
                pubsub = _redis.pubsub()
                await pubsub.subscribe(output_channel)
                logger.debug(f"Output relay subscribed: {output_channel}")
                async for msg in pubsub.listen():
                    if msg["type"] != "message":
                        continue
                    try:
                        await websocket.send_text(msg["data"])
                    except Exception:
                        pass  # 客户端可能已断开，不 crash
            except Exception as e:
                logger.warning(f"Output relay error, reconnecting: {e}")
                await _asyncio.sleep(1)

    relay_task = _asyncio.create_task(_output_relay())

    # ── Redis LPUSH helper ──────────────────────────────
    async def _push_to_redis(msg: dict):
        """推送消息到 Redis 队列，失败重试 3 次。"""
        msg["_session_id"] = session_id
        msg["_agent_id"] = agent_id
        data = _json.dumps(msg, ensure_ascii=False, default=str)
        for attempt in range(3):
            try:
                await _redis.lpush(ws_queue, data)
                return True
            except Exception as e:
                logger.warning(f"Redis LPUSH attempt {attempt+1}/3: {e}")
                await _asyncio.sleep(0.5)
        return False

    # ── 主消息循环 (永不主动断开) ──────────────────────
    while True:
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            logger.info(f"WS client disconnected: agent={agent_id}, session={session_id}")
            break
        except Exception as e:
            logger.warning(f"WS recv error: {e}, continuing...")
            await _asyncio.sleep(0.5)
            continue

        # 解析 JSON
        try:
            msg = _json.loads(raw)
        except _json.JSONDecodeError:
            continue  # 忽略无效 JSON，不断线

        msg_type = msg.get("type", "")

        # 心跳: ping → pong
        if msg_type == "ping":
            try:
                await websocket.send_text(_json.dumps({
                    "type": "pong",
                    "role": "assistant",
                    "agentId": agent_id,
                    "sessionId": session_id,
                }, ensure_ascii=False))
            except Exception:
                pass
            continue

        # 所有其他消息 → LPUSH Redis
        await _push_to_redis(msg)

    # ── 清理 ────────────────────────────────────────────
    relay_task.cancel()
    try:
        await relay_task
    except _asyncio.CancelledError:
        pass
    if _redis:
        await _redis.close()
    await ws_manager.disconnect(agent_id, session_id, websocket)



# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    import uvicorn
    uvicorn.run("paper_search.api.app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
