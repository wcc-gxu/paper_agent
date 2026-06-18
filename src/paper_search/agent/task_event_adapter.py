"""TaskEventAdapter — 任务事件 → WS 协议信封构建器 (EventBus 版)。

Phase 2 重写: 不再持有 send_fn 回调，改为向 EventBus 发布事件。
ws_handler 通过订阅 EventBus 或 Daemon 的 send_fn 消费这些事件。

用法:
    adapter = TaskEventAdapter(agent_id="agent-001", session_id="main", bus=event_bus)
    await adapter.on_task_started("task-001", "入库 Transformer", "foreground", 7)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskEventAdapter:
    """将任务生命周期事件转换为 WS 协议信封，发布到 EventBus。

    5 种 task 子类 + 2 种 notification 子类。

    支持两种使用模式:
      1. EventBus 模式 (Phase 2 推荐): 传入 bus，事件发布到 EventBus
      2. 直接回调模式 (向后兼容): 传入 send_fn，事件直接回调发送
    """

    def __init__(self, agent_id: str, session_id: str,
                 send_fn: Any = None,
                 bus: Any = None):
        """
        Args:
            agent_id: Agent 部署实例标识
            session_id: 会话标识
            send_fn: [向后兼容] 异步回调 async def(envelope: dict)
            bus: [Phase 2] EventBus 实例
        """
        self.agent_id = agent_id
        self.session_id = session_id
        self._send = send_fn
        self._bus = bus

    # ── 信封构建器 ─────────────────────────────────────────

    def _env(self, type_: str, sub_type: str, payload: dict,
             priority: int = 1) -> dict:
        """构建符合协议的信封。"""
        return {
            "role": "assistant",
            "type": type_,
            "subType": sub_type,
            "agentId": self.agent_id,
            "sessionId": self.session_id,
            "seq": 0,
            "priority": priority,
            "timestamp": _now(),
            "payload": payload,
        }

    async def _emit(self, envelope: dict, event_type: str = ""):
        """发送信封（EventBus 优先，降级到 send_fn）。"""
        if self._bus is not None:
            try:
                from .event_bus import CeleryProgressEvent, Priority
                await self._bus.push(CeleryProgressEvent(
                    agent_id=self.agent_id,
                    session_id=self.session_id,
                    agent_type="ingest",
                    data=envelope.get("payload", {}),
                ), priority=envelope.get("priority", Priority.CELERY_PROGRESS))
                return
            except Exception as e:
                logger.debug(f"EventBus push failed, falling back to send_fn: {e}")

        if self._send is not None:
            try:
                await self._send(envelope)
            except Exception as e:
                logger.error(f"TaskEvent emit failed: {e}")

    # ── task 类型 5 种子类 ─────────────────────────────────

    async def on_task_started(self, task_id: str, name: str,
                              mode: str = "foreground",
                              total_stages: int = 7):
        """任务创建 → task(started)。"""
        await self._emit(self._env("task", "started", {
            "taskId": task_id,
            "name": name,
            "mode": mode,
            "totalStages": total_stages,
        }))

    async def on_task_running(self, task_id: str, stage: str,
                              stage_index: int, total_stages: int,
                              current: int, total: int,
                              mode: str = "foreground"):
        """任务进度 → task(running)。"""
        await self._emit(self._env("task", "running", {
            "taskId": task_id,
            "mode": mode,
            "stage": stage,
            "stageIndex": stage_index,
            "totalStages": total_stages,
            "current": current,
            "total": total,
        }))

    async def on_task_backgrounded(self, task_id: str,
                                    reason: str = "user_new_message"):
        """前台→后台（不可逆） → task(backgrounded) + message(notification)。"""
        await self._emit(self._env("task", "backgrounded", {
            "taskId": task_id,
            "reason": reason,
        }))
        await self._emit(self._env("message", "notification", {
            "title": "转入后台",
            "body": f"任务 {task_id} 已转入后台继续执行",
            "category": "task_backgrounded",
            "data": {"taskId": task_id},
        }))

    async def on_task_done(self, task_id: str, result: dict):
        """任务完成 → task(done) + message(notification, task_complete)。"""
        total = result.get("total_papers", result.get("total", 0))
        downloaded = result.get("downloaded", 0)
        failed = result.get("failed", 0)
        await self._emit(self._env("task", "done", {
            "taskId": task_id,
            "result": result,
        }))
        await self._emit(self._env("message", "notification", {
            "title": "任务完成",
            "body": f"共 {total} 篇论文，{downloaded} 篇已下载" +
                    (f"，{failed} 篇失败" if failed else ""),
            "category": "task_complete",
            "data": {"taskId": task_id},
        }))

    async def on_task_failed(self, task_id: str, error: str):
        """任务失败 → task(failed)。"""
        await self._emit(self._env("task", "failed", {
            "taskId": task_id,
            "error": error,
        }))
