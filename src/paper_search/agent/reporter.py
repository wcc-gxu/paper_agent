"""Reporter — Celery Worker → Agent 主线程进度通知。

通过 Redis LPUSH 将进度事件推送到 agent:events 队列。
主 Agent daemon 通过 BRPOP 消费这些事件。

事件格式:
  {"type": "celery_progress", "task_id": "...", "level": "normal|high", "data": {...}}
  {"type": "celery_done", "task_id": "...", "result": {...}}
  {"type": "celery_error", "task_id": "...", "error": "..."}
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class Reporter:
    """Celery → Agent 进度上报。

    level 语义:
      - "normal" → Agent 缓存，更新 iOS 状态
      - "high"   → Agent 立即喂给 LLM（如需要用户决策的错误）
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self._redis_url = redis_url
        self._redis = None
        self._queue = "agent:events"

    @property
    def redis(self):
        """惰性初始化 Redis 连接。"""
        if self._redis is None:
            import redis as _redis
            self._redis = _redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    def _push(self, event: dict):
        """将事件推送到 Redis 队列。"""
        try:
            self.redis.lpush(self._queue, json.dumps(event, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"Failed to push event to Redis: {e}")

    def report_progress(self, task_id: str, level: str, data: dict):
        """报告 Celery 任务进度。

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
        """报告 Celery 任务完成。

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
        """报告 Celery 任务失败。

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
