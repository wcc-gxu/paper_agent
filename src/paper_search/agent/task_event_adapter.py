"""TaskEventAdapter — 任务事件 → WS 协议信封构建器。

作为 PipelineRunner/IngestAgent 与 WebSocket 层之间的桥梁。
持有 send_fn 回调（由 app.py 注入），负责构建符合
websocket-protocol.md v7.0 的 task/message(notification) 信封并发送。

用法:
    adapter = TaskEventAdapter(
        agent_id="agent-001", session_id="main",
        send_fn=ws_manager.send_and_persist,
    )
    await adapter.on_task_started("task-001", "入库 Transformer", "foreground", 7)
    await adapter.on_task_running("task-001", "搜索论文", 1, 7, 18, 50)
    await adapter.on_task_done("task-001", {"total": 50, "downloaded": 48, "failed": 2})
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskEventAdapter:
    """将任务生命周期事件转换为 WS 协议信封并发送。

    5 种 task 子类 + 2 种 notification 子类。
    """

    def __init__(self, agent_id: str, session_id: str,
                 send_fn: Callable[..., Any] = None):
        """
        Args:
            agent_id: Agent 部署实例标识
            session_id: 会话标识
            send_fn: 异步回调 async def(envelope: dict) — 发送 + 持久化
        """
        self.agent_id = agent_id
        self.session_id = session_id
        self._send = send_fn

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

    async def _emit(self, envelope: dict):
        """发送信封（若 send_fn 已注入）。"""
        if self._send is None:
            logger.debug(f"TaskEvent: no send_fn, dropping {envelope.get('type')}/{envelope.get('subType')}")
            return
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
        # 1) task(backgrounded)
        await self._emit(self._env("task", "backgrounded", {
            "taskId": task_id,
            "reason": reason,
        }))
        # 2) notification
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
