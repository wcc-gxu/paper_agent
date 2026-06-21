"""Reporter — Celery Worker → Agent 主线程进度通知。

双通道:
  1. Redis LPUSH → agent:events:{agent_id}  (Daemon BRPOP 消费)
     - 用于 Celery 任务完成/错误通知
  2. Redis Pub/Sub → agent:reports:{task_id} (Daemon SUBSCRIBE 消费)
     - 用于子 Agent 实时进度报告 (每篇论文下载/转换/索引状态)

事件格式 (LPUSH):
  {"type": "celery_progress", "task_id": "...", "level": "normal|high", "data": {...}}
  {"type": "celery_done", "task_id": "...", "result": {...}}
  {"type": "celery_error", "task_id": "...", "error": "..."}

报告格式 (Pub/Sub):
  {"task_id": "...", "agent_type": "ingest", "stage": "download",
   "paper_index": 5, "paper_total": 50, "paper_id": "...",
   "status": "done", "timestamp": "2026-06-18T10:30:00Z"}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Reporter:
    """Celery → Agent 进度上报。

    level 语义:
      - "normal" → Agent 缓存，更新 iOS 状态
      - "high"   → Agent 立即喂给 LLM（如需要用户决策的错误）

    双通道:
      - LPUSH → agent:events:{agent_id}  (任务完成/错误)
      - PUBLISH → agent:reports:{task_id} (实时进度)
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 agent_id: str = "agent-001"):
        self._redis_url = redis_url
        self._agent_id = agent_id
        self._redis = None
        self._events_queue = f"agent:events:{agent_id}"

    @property
    def redis(self):
        """惰性初始化 Redis 连接。"""
        if self._redis is None:
            import redis as _redis
            self._redis = _redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    # ── LPUSH (任务完成/错误) ─────────────────────────────

    def _push(self, event: dict):
        """将事件推送到 Redis LPUSH 队列。"""
        try:
            self.redis.lpush(self._events_queue,
                           json.dumps(event, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"Failed to push event to Redis: {e}")

    def report_progress(self, task_id: str, level: str, data: dict):
        """报告 Celery 任务进度 (LPUSH)。

        Args:
            task_id: 任务 ID
            level: "normal" | "high"
            data: 进度数据（stage, paper_id, status 等）
        """
        self._push({
            "type": "celery_progress",
            "task_id": task_id,
            "level": level,
            "data": data,
        })
        if level == "high":
            logger.info(f"[REPORT HIGH] task={task_id} data={json.dumps(data, ensure_ascii=False)[:200]}")

    def report_done(self, task_id: str, result: dict):
        """报告 Celery 任务完成 (LPUSH)。

        Args:
            task_id: 任务 ID
            result: 最终结果
        """
        self._push({
            "type": "celery_done",
            "task_id": task_id,
            "result": result,
        })
        logger.info(f"[REPORT DONE] task={task_id}")

    def report_error(self, task_id: str, error: str):
        """报告 Celery 任务失败 (LPUSH)。

        Args:
            task_id: 任务 ID
            error: 错误信息
        """
        self._push({
            "type": "celery_error",
            "task_id": task_id,
            "error": error,
        })
        logger.error(f"[REPORT ERROR] task={task_id} error={error[:200]}")

    # ── Pub/Sub (实时进度) ─────────────────────────────────

    def publish_report(self, task_id: str, agent_type: str, stage: str,
                       paper_index: int = 0, paper_total: int = 0,
                       paper_id: str = "", status: str = "progress",
                       extra: dict = None):
        """发布子 Agent 实时报告 (Pub/Sub → agent:reports:{task_id})。

        Args:
            task_id: 任务 ID
            agent_type: 子 Agent 类型 (ingest / citation_chase / ...)
            stage: 当前阶段 (search / download / convert / index / ...)
            paper_index: 当前论文索引 (1-based)
            paper_total: 总论文数
            paper_id: 论文 ID
            status: 状态 (start / done / failed / retrying)
            extra: 额外数据
        """
        channel = f"agent:reports:{task_id}"
        message = {
            "type": "progress",  # 区分于 lifecycle
            "task_id": task_id,
            "agent_type": agent_type,
            "stage": stage,
            "paper_index": paper_index,
            "paper_total": paper_total,
            "paper_id": paper_id,
            "status": status,
            "timestamp": _now(),
        }
        if extra:
            message.update(extra)

        try:
            self.redis.publish(channel, json.dumps(message, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"Failed to publish report to {channel}: {e}")

    def publish_lifecycle(self, task_id: str, agent_type: str,
                          lifecycle: str, summary: str = "",
                          result: dict = None, error: str = ""):
        """发布子 Agent 生命周期事件 (Pub/Sub → agent:reports:{task_id})。

        与 publish_report 的"per-stage progress"不同，lifecycle 事件代表
        整个子 Agent 的"启动 / 完成 / 失败"，由订阅方用来判定子 Agent
        是否真的结束（替代 v2 误判 per-paper status=done 的 Bug）。

        Args:
            task_id: 任务 ID
            agent_type: 子 Agent 类型
            lifecycle: "agent_started" | "agent_done" | "agent_failed"
            summary: 简短描述（给 iOS 展示）
            result: 完整结果（done 时）
            error: 错误信息（failed 时）
        """
        channel = f"agent:reports:{task_id}"
        message = {
            "type": "lifecycle",  # 关键标记：订阅方依赖此字段判定完成
            "task_id": task_id,
            "agent_type": agent_type,
            "lifecycle": lifecycle,
            "summary": summary,
            "result": result or {},
            "error": error,
            "timestamp": _now(),
        }
        try:
            self.redis.publish(channel, json.dumps(message, ensure_ascii=False, default=str))
            logger.info(f"[LIFECYCLE] task={task_id} agent={agent_type} {lifecycle}")
        except Exception as e:
            logger.warning(f"Failed to publish lifecycle to {channel}: {e}")

    def publish_notification(self, notification: dict):
        """发布跨进程通知到 agent:notifications channel。

        Celery Worker → API 进程的桥接通道。
        API 进程的 start_notification_listener() 订阅此 channel。

        Args:
            notification: 通知数据 dict，含 subscription_id/new_papers 等
        """
        channel = "agent:notifications"
        try:
            self.redis.publish(
                channel,
                json.dumps(notification, ensure_ascii=False, default=str),
            )
            logger.debug(
                f"Published notification to {channel}: "
                f"sub={notification.get('subscription_id', '?')}"
            )
        except Exception as e:
            logger.warning(f"Failed to publish notification to {channel}: {e}")

