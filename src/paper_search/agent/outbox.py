"""Outbox — 出站消息统一封装 (Phase 1)。

替代旧的 `redis.publish(agent:output:*)` fire-and-forget 模式：
  - 双写: SQLite ws_messages（持久化）+ Redis List outbox:{agent_id}（队列）
  - API 进程的 outbox_poller 协程从 Redis BRPOP 取出 → WS 在线推 / 离线 APNs
  - 失败重投: send 失败的消息 LPUSH 回队列头部
  - 离线同步: iOS 重连后通过 sync_request 拉取 ws_messages 中未送达的消息

priority_kind 重要性等级（决定 APNs 策略）:
  - silent  : thinking 流式 delta → 不持久化、不 APNs
  - normal  : tool 进度等普通信息 → 持久化、不 APNs
  - high    : 任务完成 / plan 卡片 / 澄清问题 → 持久化、APNs（带预览）
  - urgent  : 错误 / 订阅推送 → 持久化、APNs（带响铃）

使用方式::

    from paper_search.agent.outbox import outbox_publish

    await outbox_publish(
        redis_client, db, envelope,
        correlation_id=current_turn_id,
    )
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── 重要性默认值 (type/subType → priority_kind) ───────────────

PRIORITY_DEFAULTS: dict[tuple[str, str], str] = {
    # 流式 thinking → 不持久化
    ("message", "thinking"): "silent",
    # 最终文本回复
    ("message", "text"): "high",
    ("message", "reply"): "high",
    # 工具调用
    ("tool", "sub_request"): "high",
    ("tool", "sub_progress"): "normal",
    ("tool", "sub_result"): "high",
    ("tool", "ask_user_question"): "high",
    ("tool", "propose_plan"): "high",
    ("tool", "ios_request"): "high",
    ("tool", "ios_result"): "normal",
    # 错误
    ("error", "TASK_FAILED"): "urgent",
    ("error", "INTERNAL_ERROR"): "urgent",
    ("error", "MAX_ROUNDS"): "high",
    # 心跳类 (理论上不应走 outbox)
    ("ping", ""): "silent",
    ("pong", ""): "silent",
}


def infer_priority(envelope: dict) -> str:
    """根据 envelope 的 type/subType 推断 priority_kind。

    envelope 已含 priorityKind 字段则尊重它。
    """
    explicit = envelope.get("priorityKind")
    if explicit in ("silent", "normal", "high", "urgent"):
        return explicit

    msg_type = envelope.get("type", "")
    sub_type = envelope.get("subType", "") or ""
    return PRIORITY_DEFAULTS.get(
        (msg_type, sub_type),
        PRIORITY_DEFAULTS.get((msg_type, ""), "normal"),
    )


def outbox_key(agent_id: str) -> str:
    """Outbox List 在 Redis 中的 key。"""
    return f"outbox:{agent_id}"


async def outbox_publish(
    redis_client: Any,
    db: Any,
    envelope: dict,
    correlation_id: str = "",
) -> str:
    """发布一条出站消息到 outbox（持久化 + 排队）。

    Args:
        redis_client: redis.asyncio 客户端
        db: AgentDB 实例
        envelope: 完整 v9.0 协议信封（type/subType/role/agentId/sessionId/...）
        correlation_id: 关联一轮对话的 ID（事件源 Checkpoint 用）

    Returns:
        msg_id（UUID 字符串，可用于后续 mark_delivered）

    流式 thinking（priorityKind=silent）只入 Redis 队列，不写 SQLite，
    避免每个 delta 都打一次 IO。
    """
    if not envelope.get("msg_id"):
        envelope["msg_id"] = str(uuid.uuid4())
    if not envelope.get("timestamp"):
        envelope["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    priority_kind = infer_priority(envelope)
    envelope["priorityKind"] = priority_kind
    msg_id = envelope["msg_id"]
    agent_id = envelope.get("agentId", "")

    # 1) 写 SQLite（silent 跳过以节省 IO）
    if priority_kind != "silent" and db is not None:
        try:
            db.save_outbox_envelope(envelope, correlation_id=correlation_id)
        except Exception as e:
            # 持久化失败不应阻塞推送，仅 log
            logger.warning(f"outbox: SQLite write failed for {msg_id}: {e}")

    # 2) LPUSH Redis outbox List
    try:
        data = json.dumps(envelope, ensure_ascii=False, default=str)
        await redis_client.lpush(outbox_key(agent_id), data)
    except Exception as e:
        logger.error(f"outbox: Redis LPUSH failed for {msg_id}: {e}")
        # Redis 失败仍返回 msg_id；如果 SQLite 写成功了，iOS 重连可拉取
        return msg_id

    return msg_id


def outbox_publish_sync(
    redis_client: Any,
    db: Any,
    envelope: dict,
    correlation_id: str = "",
) -> str:
    """同步版 outbox_publish — 给 Celery worker 用（reporter.py）。

    用法同 outbox_publish，但 redis_client 是同步 redis-py 客户端。
    """
    if not envelope.get("msg_id"):
        envelope["msg_id"] = str(uuid.uuid4())
    if not envelope.get("timestamp"):
        envelope["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    priority_kind = infer_priority(envelope)
    envelope["priorityKind"] = priority_kind
    msg_id = envelope["msg_id"]
    agent_id = envelope.get("agentId", "")

    if priority_kind != "silent" and db is not None:
        try:
            db.save_outbox_envelope(envelope, correlation_id=correlation_id)
        except Exception as e:
            logger.warning(f"outbox(sync): SQLite write failed for {msg_id}: {e}")

    try:
        data = json.dumps(envelope, ensure_ascii=False, default=str)
        redis_client.lpush(outbox_key(agent_id), data)
    except Exception as e:
        logger.error(f"outbox(sync): Redis LPUSH failed for {msg_id}: {e}")
        return msg_id

    return msg_id
