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
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .routes import router
from .ws import ws_manager

logger = logging.getLogger(__name__)
# 确保 WS 连接日志可见（uvicorn 只启用自己的 handler，根 logger 是 WARNING）
_pkg_logger = logging.getLogger("paper_search.api")
_pkg_logger.setLevel(logging.INFO)
if not _pkg_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _pkg_logger.addHandler(_h)
    _pkg_logger.propagate = False


# ═══════════════════════════════════════════════════════════════
# Lifespan — 惰性初始化
# ═══════════════════════════════════════════════════════════════


_db = None
_engine = None
_llm = None
_chroma = None
_kb = None
_capabilities_cache: dict = {}  # (agent_id, session_id) → list[str]


def get_db():
    global _db
    if _db is None:
        from ..config import use_postgresql
        if use_postgresql():
            from ..agent.pgdb import PostgresAgentDB
            _db = PostgresAgentDB()
        else:
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
        from ..config import use_postgresql
        if use_postgresql():
            from ..agent.pgvector_store import PgVectorStore
            _chroma = PgVectorStore()
        else:
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
    try:
        from .outbox_poller import stop_all_pollers
        await stop_all_pollers()
    except Exception as e:
        logger.warning(f"stop_all_pollers failed: {e}")
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

# ── Docs 静态文件服务 ──────────────────────────────────────
_docs_dir = Path(__file__).parent.parent.parent.parent / "docs"

if _docs_dir.exists():
    # 挂载 /paper/docs/files → 提供原始文件下载
    app.mount("/paper/docs/files", StaticFiles(directory=str(_docs_dir)), name="docs_files")

    @app.get("/paper/docs", response_class=HTMLResponse)
    async def docs_index():
        """自动生成文档索引页 — 列出所有 .md 文件，显示更新时间，提供下载。"""
        import time as _time
        md_files = sorted(
            [f for f in _docs_dir.rglob("*.md") if f.is_file()],
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        rows = ""
        for f in md_files:
            rel = f.relative_to(_docs_dir)
            mtime = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(f.stat().st_mtime))
            size_kb = f.stat().st_size / 1024
            rows += f"""
            <tr>
              <td><code>{rel}</code></td>
              <td>{mtime}</td>
              <td>{size_kb:.1f} KB</td>
              <td><a href="/paper/docs/files/{rel}" download class="btn">⬇ 下载</a></td>
            </tr>"""
        return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Agent v3 — 文档</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0d1117; color:#c9d1d9; padding:2rem; }}
  h1 {{ color:#58a6ff; margin-bottom:.5rem; }}
  .updated {{ color:#8b949e; font-size:.9rem; margin-bottom:1.5rem; }}
  table {{ width:100%; border-collapse:collapse; }}
  th {{ text-align:left; padding:.75rem .5rem; border-bottom:1px solid #30363d; color:#8b949e; font-weight:600; font-size:.85rem; }}
  td {{ padding:.75rem .5rem; border-bottom:1px solid #21262d; }}
  tr:hover {{ background:#161b22; }}
  code {{ color:#d2a8ff; font-size:.9rem; }}
  .btn {{ display:inline-block; padding:.35rem .85rem; border-radius:6px; background:#238636; color:#fff; text-decoration:none; font-size:.85rem; font-weight:600; }}
  .btn:hover {{ background:#2ea043; }}
  .desc {{ color:#8b949e; font-size:.85rem; margin-bottom:1rem; }}
</style>
</head>
<body>
<h1>Paper Agent v3 — 文档</h1>
<p class="updated">🕐 自动生成 · 文件时间戳同步 · 无需手动维护</p>
<p class="desc">{len(md_files)} 个文档文件。点击下载按钮获取最新版本。</p>
<table>
<thead><tr><th>文件</th><th>更新时间</th><th>大小</th><th>操作</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws/chat/{agent_id}/{session_id}")
async def ws_chat(websocket: WebSocket, agent_id: str, session_id: str,
                  token: str = Query(None)):
    """WebSocket 中继 — v10.1 协议 + JWT 认证 + Phase 1 outbox 模式。

    - 连接: ws://host/ws/chat/{agent_id}/{session_id}?token=<jwt>
    - JWT 验证: token 无效则拒绝连接 (code 4001)
    - 连接即用，不需要额外握手
    - 收消息 → LPUSH agent:ws:{agent_id} → Daemon 消费
    - Daemon 通过 outbox_publish 写消息 → API 进程的 outbox_poller
      从 outbox:{agent_id} BRPOP → 这里 send_text
    - sync_request: 客户端发 sync_request → 拉取未送达的历史消息回放
    - 永不主动断开连接

    v3 Phase 1: JWT 验证后 user_id 来自 token payload。
    """
    import json as _json
    import os
    import asyncio as _asyncio

    # ── JWT 验证 (优先) ──────────────────────────────────
    from .auth import verify_ws_token
    try:
        _user_id = await verify_ws_token(websocket, agent_id, token)
    except HTTPException:
        # verify_ws_token 已经关闭了 WebSocket
        return

    await websocket.accept()
    await ws_manager.connect(agent_id, session_id, websocket)
    logger.info(f"WS connected: agent={agent_id}, session={session_id}, user={_user_id}")

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

    # ── Phase 1: 启动 outbox_poller (每个 agent 一个，幂等) ──────
    from .outbox_poller import start_poller
    db = get_db()
    poller_task = start_poller(agent_id, ws_manager, db,
                                redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    logger.info("📡 OutboxPoller running for agent=%s", agent_id)

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

    # ── Phase 1: sync_request 处理（拉历史消息） ──────────
    async def _handle_sync_request(msg: dict):
        """iOS 重连后请求拉取未送达的历史消息。

        请求格式: {type: "sync_request", payload: {last_msg_id?: "..."}}
        响应: 多条历史 envelope + {type: "sync_complete", payload: {synced_count: N}}
        """
        payload = msg.get("payload", {}) or {}
        last_msg_id = payload.get("last_msg_id", "")
        try:
            history = db.get_undelivered_messages(
                agent_id, session_id, since_msg_id=last_msg_id,
            )
        except Exception as e:
            logger.warning(f"sync_request: get_undelivered failed: {e}")
            history = []

        synced = 0
        for env in history:
            try:
                await websocket.send_text(_json.dumps(env, ensure_ascii=False, default=str))
                mid = env.get("msg_id", "")
                if mid:
                    try:
                        db.mark_message_delivered(mid, session_id)
                    except Exception:
                        pass
                synced += 1
            except Exception as e:
                logger.warning(f"sync_request: send failed at #{synced}: {e}")
                break

        try:
            await websocket.send_text(_json.dumps({
                "type": "sync_complete",
                "role": "assistant",
                "agentId": agent_id,
                "sessionId": session_id,
                "payload": {"synced_count": synced},
            }, ensure_ascii=False))
        except Exception:
            pass
        logger.info("🔄 SYNC | agent=%s sess=%s replayed=%d", agent_id, session_id, synced)

    # ── 主消息循环 (永不主动断开) ──────────────────────
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            client_ip = ""
            try:
                c = getattr(websocket, "client", None)
                if c:
                    client_ip = f"{c.host}:{c.port}"
            except Exception:
                pass
            logger.info(
                "🔌 WS DISCONNECT | agent=%s session=%s client=%s",
                agent_id, session_id, client_ip or "unknown",
            )
            break
        except Exception as e:
            logger.warning(f"WS recv error: {e}, continuing...")
            await _asyncio.sleep(0.5)
            continue

        # 检测断开 (Starlette receive() 返回 disconnect dict，不抛异常)
        if message.get("type") == "websocket.disconnect":
            logger.info(
                "🔌 WS DISCONNECT | agent=%s session=%s code=%s",
                agent_id, session_id, message.get("code", "?"),
            )
            break

        # 兼容 text 和 binary 帧（iOS 可能发送 binary ping/message）
        if "text" in message:
            raw = message["text"]
        elif "bytes" in message:
            raw = message["bytes"].decode("utf-8", errors="replace")
            logger.info("📩 WS RECV binary (%d bytes): %s", len(message["bytes"]), raw[:200])
        else:
            # WebSocket 控制帧 (如 protocol-level ping/pong)，忽略
            logger.info("📡 WS control frame: type=%s", message.get("type", "?"))
            continue

        # 解析 JSON
        try:
            msg = _json.loads(raw)
        except _json.JSONDecodeError:
            logger.warning("📩 WS RECV non-JSON (%d chars): %s", len(raw), raw[:200])
            continue  # 忽略无效 JSON，不断线

        msg_type = msg.get("type", "")
        sub_type = msg.get("subType", "")
        logger.info(
            "📩 WS RECV | type=%s%s seq=%s size=%d",
            msg_type,
            f"/{sub_type}" if sub_type else "",
            msg.get("seq", 0),
            len(raw),
        )

        # v10: 缓存信封级 capabilities（每条 inbound 都可能带）
        caps = msg.get("capabilities")
        if isinstance(caps, list):
            _capabilities_cache[(agent_id, session_id)] = list(caps)

        # 心跳: ping → pong（兼容 type:ping 和 type:heartbeat/subType:ping）
        if msg_type == "ping" or (msg_type == "heartbeat" and sub_type == "ping"):
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

        # v10: sync (新名) + v9: sync_request (旧名) → 回放未送达历史
        if msg_type in ("sync", "sync_request"):
            await _handle_sync_request(msg)
            continue

        # v10 入站类型 → 转写为 main_agent 期望的 v9 形态再 LPUSH
        # (main_agent._wait_ws_reply 已对 v10 type 做了 alias，但保险起见
        # 在这里把"无 subType 的 message"补成 v9 message/chat，便于 main_agent.run
        # 的入口聚合逻辑不变。)
        if msg_type == "message" and not sub_type:
            # v10 用户文本 → 兼容 v9 message/chat
            msg.setdefault("subType", "chat")
            msg.setdefault("role", "user")

        # message/chat / ask_reply / tool_result 等都原样 LPUSH —
        # main_agent._wait_ws_reply 内部 v10 别名匹配会处理；
        # 入口 BRPOP 只看 type+subType 不强制限制。

        # 所有其他消息 → LPUSH Redis
        await _push_to_redis(msg)

    # ── 清理 ────────────────────────────────────────────
    # poller_task 不在这里 stop（其他 session 可能还要用），由 lifespan 关闭
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
