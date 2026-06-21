"""Outbox Poller — Redis outbox List → WebSocket / APNs 分发 (Phase 1)。

每个 agent_id 一个 poller 协程。从 outbox:{agent_id} BRPOP 取消息：
  - 该 agent 有在线 WS session → 发送 + mark_delivered
  - 该 agent 无在线 session → 按 priority_kind 决定是否 APNs
  - WS send 失败 → 重新 LPUSH 到队列头部（保留次序）

启动时机:
  - 在 FastAPI lifespan 中为每个已知 agent_id 启动一个 poller
  - 或者第一个 WS 连接到该 agent_id 时按需启动

在多 agent 场景下也工作良好（每个 agent 独立队列 + 独立 poller）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from ..agent.outbox import outbox_key
from .apns_pusher import get_apns_pusher

logger = logging.getLogger(__name__)


# 每个 agent_id 对应一个 poller task
_pollers: dict[str, asyncio.Task] = {}


async def _poller_loop(agent_id: str, ws_manager: Any, db: Any, redis_url: str):
    """单个 agent 的 outbox 消费循环。"""
    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(redis_url, decode_responses=True)
    key = outbox_key(agent_id)
    pusher = get_apns_pusher(db=db)

    logger.info("OutboxPoller started: agent=%s key=%s", agent_id, key)

    try:
        while True:
            try:
                raw = await redis_client.brpop(key, timeout=0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("OutboxPoller BRPOP error for %s: %s", agent_id, e)
                await asyncio.sleep(1)
                continue

            if raw is None:
                continue

            data = raw[1]
            try:
                envelope = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("OutboxPoller: malformed envelope, dropping: %s", data[:200])
                continue

            await _dispatch_envelope(envelope, ws_manager, db, pusher, redis_client, key)

    except asyncio.CancelledError:
        logger.info("OutboxPoller cancelled: agent=%s", agent_id)
        raise
    finally:
        try:
            await redis_client.close()
        except Exception:
            pass


async def _dispatch_envelope(envelope: dict, ws_manager: Any, db: Any,
                              pusher: Any, redis_client: Any, key: str):
    """分发一条 envelope: WS 在线 → 推送 + mark_delivered；离线 → APNs。"""
    agent_id = envelope.get("agentId", "")
    session_id = envelope.get("sessionId", "main")
    msg_id = envelope.get("msg_id", "")
    msg_type = envelope.get("type", "?")
    sub_type = envelope.get("subType", "")
    priority_kind = envelope.get("priorityKind", "normal")

    online_sessions = ws_manager.get_online_sessions(agent_id) if hasattr(ws_manager, "get_online_sessions") else []

    # 决定哪些 WS 连接需要接收
    # sessionId == "main" 视为广播到所有 session；否则仅该 session
    target_sessions = []
    if online_sessions:
        if session_id == "main":
            target_sessions = list(online_sessions)
        elif session_id in online_sessions:
            target_sessions = [session_id]

    if target_sessions:
        data_str = json.dumps(envelope, ensure_ascii=False, default=str)
        for sess_id in target_sessions:
            ws = ws_manager.get_websocket(agent_id, sess_id) if hasattr(ws_manager, "get_websocket") else None
            if ws is None:
                continue
            try:
                await ws.send_text(data_str)
                logger.info(
                    "📤 OUTBOX→WS | agent=%s sess=%s type=%s/%s msg=%s",
                    agent_id, sess_id, msg_type, sub_type, msg_id[:8],
                )
                # 标记送达
                if msg_id and db is not None and priority_kind != "silent":
                    try:
                        db.mark_message_delivered(msg_id, sess_id)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("OutboxPoller WS send failed (sess=%s): %s; requeueing", sess_id, e)
                # 重新塞回队列头（保留次序）
                try:
                    await redis_client.lpush(key, data_str)
                except Exception:
                    pass
                return

    else:
        # 离线 → 看是否触发 APNs
        if priority_kind in ("high", "urgent") and pusher and agent_id:
            silent = (priority_kind == "high" and msg_type == "tool"
                      and sub_type == "sub_progress")
            try:
                await pusher.push(agent_id, envelope, silent=silent)
            except Exception as e:
                logger.warning("APNs push failed for %s: %s", msg_id[:8], e)
        else:
            # normal / silent 消息离线时仅写库等同步
            logger.debug(
                "[OUTBOX OFFLINE] agent=%s msg=%s priority=%s — stored only",
                agent_id, msg_id[:8], priority_kind,
            )


def start_poller(agent_id: str, ws_manager: Any, db: Any,
                 redis_url: Optional[str] = None) -> asyncio.Task:
    """为 agent_id 启动一个 outbox poller（幂等：已存在则返回原 task）。"""
    if agent_id in _pollers and not _pollers[agent_id].done():
        return _pollers[agent_id]
    redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    task = asyncio.create_task(
        _poller_loop(agent_id, ws_manager, db, redis_url),
        name=f"outbox-poller-{agent_id}",
    )
    _pollers[agent_id] = task
    return task


async def stop_poller(agent_id: str):
    """停止指定 agent_id 的 poller。"""
    task = _pollers.pop(agent_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def stop_all_pollers():
    """关闭全部 poller（lifespan shutdown 调）。"""
    for agent_id in list(_pollers.keys()):
        await stop_poller(agent_id)


def get_running_pollers() -> list[str]:
    """诊断: 列出当前运行中的 poller。"""
    return [aid for aid, t in _pollers.items() if not t.done()]
