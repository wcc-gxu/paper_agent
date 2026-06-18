"""事件总线 — AgentRunLoop Tick-Polling 模式。

架构 (对齐 iOS CFRunLoop):
  源检查顺序 = 优先级，不再需要 PriorityQueue 排序。

  源 0 (prio=0): WebSocket — 用户交互
  源 1 (prio=1): Redis LPOP — Celery 完成/错误 (跨进程)
  源 2 (prio=2): Redis Pub/Sub — 子Agent 实时报告 (跨进程)
  源 3 (prio=3): Timer — 定时任务触发

  每个源提供非阻塞 drain()/pop_nowait()/pop_fired() 接口。
  RunLoop 按固定顺序遍历，所有源都空时才 sleep(TICK)。

双向通信:
  子→主: Redis Pub/Sub agent:reports:{task_id}  (实时进度 + 生命周期事件)
  主→子: Redis Pub/Sub agent:cmd:{task_id}     (启动/暂停/取消指令)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 优先级 — 源检查顺序即优先级
# ═══════════════════════════════════════════════════════════════


class Priority(IntEnum):
    USER = 0
    CELERY_DONE = 1
    CELERY_PROGRESS = 2
    TIMER = 3


# ═══════════════════════════════════════════════════════════════
# 事件类型
# ═══════════════════════════════════════════════════════════════


class EventType:
    USER_MESSAGE = "user_message"
    USER_CLARIFICATION = "user_clarification"
    USER_APPROVAL = "user_approval"
    IOS_TOOL_RESULT = "ios_tool_result"
    CELERY_DONE = "celery_done"
    CELERY_ERROR = "celery_error"
    CELERY_PROGRESS = "celery_progress"
    TIMER_FIRED = "timer_fired"
    SYSTEM_SHUTDOWN = "system_shutdown"

    # 子Agent 生命周期 (Pub/Sub)
    AGENT_STARTED = "agent_started"
    AGENT_DONE = "agent_done"
    AGENT_FAILED = "agent_failed"


# ═══════════════════════════════════════════════════════════════
# 事件数据类
# ═══════════════════════════════════════════════════════════════


@dataclass
class Event:
    type: str
    priority: int = Priority.CELERY_PROGRESS
    seq: int = 0
    timestamp: float = field(default_factory=time.monotonic)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str = "agent-001"
    session_id: str = "main"


@dataclass
class UserMessageEvent(Event):
    content: str = ""
    raw_envelope: dict = field(default_factory=dict)
    def __post_init__(self):
        self.type = EventType.USER_MESSAGE
        self.priority = Priority.USER


@dataclass
class UserClarificationEvent(Event):
    answers: list[str] = field(default_factory=list)
    def __post_init__(self):
        self.type = EventType.USER_CLARIFICATION
        self.priority = Priority.USER


@dataclass
class UserApprovalEvent(Event):
    confirmed: bool = False
    modifications: dict = field(default_factory=dict)
    def __post_init__(self):
        self.type = EventType.USER_APPROVAL
        self.priority = Priority.USER


@dataclass
class CeleryResultEvent(Event):
    """Celery 完成/错误 — prio=1。"""
    celery_task_id: str = ""
    agent_task_id: str = ""
    agent_type: str = ""
    is_error: bool = False
    result: dict = field(default_factory=dict)
    error: str = ""
    def __post_init__(self):
        self.type = EventType.CELERY_ERROR if self.is_error else EventType.CELERY_DONE
        self.priority = Priority.CELERY_DONE


@dataclass
class ProgressReport:
    """子Agent 报告 — prio=2。在 Pub/Sub 源中按 task_id 合并。"""
    task_id: str = ""
    agent_type: str = ""
    stage: str = ""
    stage_index: int = 0
    total_stages: int = 0
    paper_index: int = 0
    paper_total: int = 0
    paper_id: str = ""
    status: str = "progress"
    data: dict = field(default_factory=dict)
    # 生命周期标记
    is_lifecycle: bool = False  # agent_started / agent_done / agent_failed
    lifecycle_type: str = ""    # started / done / failed


@dataclass
class TimerFiredEvent:
    """定时触发 — prio=3。"""
    timer_name: str = ""
    timer_type: str = ""
    context: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# 子Agent 订阅管理器 (Observer)
# ═══════════════════════════════════════════════════════════════


class Observer:
    """RunLoop Observer — 管理子 Agent 订阅生命周期 + 心跳超时检测。

    职责:
      - 记录活跃的 task_id → {last_seen, agent_type, status}
      - 每次 Pub/Sub 收到报告时刷新 last_seen
      - 每 tick 检查超时 (>30s 无消息 → 标记 failed)
      - agent_done/agent_failed 时清理订阅
    """

    HEARTBEAT_TIMEOUT = 30  # 秒

    def __init__(self):
        self._subscriptions: dict[str, dict] = {}

    def on_agent_started(self, task_id: str, agent_type: str):
        self._subscriptions[task_id] = {
            "last_seen": time.monotonic(),
            "agent_type": agent_type,
            "status": "alive",
            "started_at": time.monotonic(),
        }
        logger.info(f"Observer: agent started task={task_id} type={agent_type}")

    def on_agent_report(self, task_id: str):
        if task_id in self._subscriptions:
            self._subscriptions[task_id]["last_seen"] = time.monotonic()

    def on_agent_done(self, task_id: str):
        sub = self._subscriptions.pop(task_id, None)
        if sub:
            elapsed = time.monotonic() - sub.get("started_at", 0)
            logger.info(f"Observer: agent done task={task_id} elapsed={elapsed:.1f}s")

    def on_agent_failed(self, task_id: str, reason: str = ""):
        sub = self._subscriptions.pop(task_id, None)
        logger.warning(f"Observer: agent failed task={task_id} reason={reason}")

    def check_timeouts(self) -> list[str]:
        """返回心跳超时的 task_id 列表。"""
        now = time.monotonic()
        timed_out = []
        for tid, sub in list(self._subscriptions.items()):
            if sub["status"] == "alive" and (now - sub["last_seen"]) > self.HEARTBEAT_TIMEOUT:
                sub["status"] = "timeout"
                timed_out.append(tid)
                logger.warning(f"Observer: heartbeat timeout task={tid} "
                              f"last_seen={now - sub['last_seen']:.0f}s ago")
        return timed_out

    @property
    def active_tasks(self) -> list[dict]:
        return [{"task_id": tid, **sub} for tid, sub in self._subscriptions.items()
                if sub["status"] == "alive"]

    @property
    def active_count(self) -> int:
        return sum(1 for s in self._subscriptions.values() if s["status"] == "alive")


# ═══════════════════════════════════════════════════════════════
# Tick-Polling 接口 (每个源提供非阻塞取数据方法)
# ═══════════════════════════════════════════════════════════════


# ── WS 消息队列 ── 简单的 asyncio.Queue 包装 ──

class WSMessageQueue:
    """WebSocket 消息队列 — 支持非阻塞 pop_nowait()。"""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()

    async def put(self, msg: dict):
        await self._queue.put(msg)

    def pop_nowait(self) -> Optional[dict]:
        """非阻塞取一条。"""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None


# ── Redis 事件源 (Celery → 主Agent) ──


class RedisEventSource:
    """Redis FIFO 队列源 — 非阻塞取 Celery 完成/错误事件。

    队列: agent:events:{agent_id}
    reporter LPUSH (左推) + pop_nowait() RPOP (右取) = FIFO
    BRPOP 不再使用 — RunLoop 按 tick 频率自己来取。
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 agent_id: str = "agent-001"):
        self._redis_url = redis_url
        self._agent_id = agent_id
        self._queue_key = f"agent:events:{agent_id}"
        self._redis = None

    @property
    def redis(self):
        if self._redis is None:
            import redis as _redis
            self._redis = _redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    def pop_nowait(self) -> Optional[CeleryResultEvent]:
        """非阻塞取一条 Redis 事件 → CeleryResultEvent。RPOP = FIFO。"""
        try:
            raw = self.redis.rpop(self._queue_key)
            if raw is None:
                return None
            data = json.loads(raw) if isinstance(raw, str) else raw
            event_type = data.get("type", "")
            task_id = data.get("task_id", "")
            agent_type = data.get("agent_type", "")

            if event_type in ("celery_done", "celery_error"):
                return CeleryResultEvent(
                    celery_task_id=task_id,
                    agent_task_id=data.get("agent_task_id", task_id),
                    agent_type=agent_type,
                    is_error=(event_type == "celery_error"),
                    result=data.get("result", {}),
                    error=data.get("error", ""),
                )
            return None
        except Exception as e:
            logger.warning(f"RedisEventSource.pop_nowait error: {e}")
            return None


# ── Pub/Sub 源 (子Agent → 主Agent 实时报告) ──


class SubAgentReportListener:
    """Redis Pub/Sub 监听器 — 子Agent 实时报告 + 生命周期事件。

    频道:
      订阅: agent:reports:{task_id}  (子Agent → 主Agent)
      发布: agent:cmd:{task_id}     (主Agent → 子Agent)

    drain() 非阻塞取出所有待处理消息 → ProgressReport 列表。
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self._redis_url = redis_url
        self._redis = None
        self._pubsub = None
        self._active_channels: set[str] = set()

    @property
    def redis(self):
        if self._redis is None:
            import redis as _redis
            self._redis = _redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    def start(self):
        self._pubsub = self.redis.pubsub()
        logger.info("SubAgentReportListener started")

    def stop(self):
        if self._pubsub:
            self._pubsub.close()
        logger.info("SubAgentReportListener stopped")

    # ── 订阅管理 ────────────────────────────────

    async def subscribe(self, task_id: str):
        """主Agent 订阅子Agent 的报告频道。"""
        channel = f"agent:reports:{task_id}"
        if channel not in self._active_channels:
            self._pubsub.subscribe(channel)
            self._active_channels.add(channel)
            logger.info(f"Subscribed to {channel}")

    async def unsubscribe(self, task_id: str):
        """取消订阅。"""
        channel = f"agent:reports:{task_id}"
        if channel in self._active_channels:
            self._pubsub.unsubscribe(channel)
            self._active_channels.discard(channel)
            logger.info(f"Unsubscribed from {channel}")

    # ── drain(): 非阻塞取出所有待处理消息 ────────

    def drain(self) -> list[ProgressReport]:
        """非阻塞取出 Pub/Sub 中所有待处理消息。

        Returns:
            ProgressReport 列表 (含生命周期标记)
        """
        reports = []
        if self._pubsub is None:
            return reports

        try:
            while True:
                message = self._pubsub.get_message(ignore_subscribe_messages=True,
                                                    timeout=0.0)
                if message is None:
                    break
                if message.get("type") != "message":
                    continue

                data_str = message.get("data", "{}")
                data = json.loads(data_str) if isinstance(data_str, str) else data_str

                msg_type = data.get("type", "")
                is_lifecycle = msg_type in ("agent_started", "agent_done", "agent_failed")

                reports.append(ProgressReport(
                    task_id=data.get("task_id", ""),
                    agent_type=data.get("agent_type", ""),
                    stage=data.get("stage", ""),
                    stage_index=data.get("stage_index", 0),
                    total_stages=data.get("total_stages", 0),
                    paper_index=data.get("paper_index", 0),
                    paper_total=data.get("paper_total", 0),
                    paper_id=data.get("paper_id", ""),
                    status=data.get("status", "progress"),
                    data=data,
                    is_lifecycle=is_lifecycle,
                    lifecycle_type=msg_type.replace("agent_", ""),
                ))
        except Exception as e:
            logger.warning(f"SubAgentReportListener.drain error: {e}")

        return reports

    # ── 生命周期发布 (发布到 agent:reports:{task_id}) ──

    def publish_lifecycle(self, task_id: str, event_type: str, agent_type: str = "",
                          extra: dict = None):
        """发布子Agent 生命周期事件到报告频道。

        Args:
            task_id: 任务 ID
            event_type: agent_started | agent_done | agent_failed
            agent_type: 子 Agent 类型
            extra: 额外数据 (result / error)
        """
        channel = f"agent:reports:{task_id}"
        msg = {
            "task_id": task_id,
            "agent_type": agent_type,
            "type": event_type,
            "timestamp": _now(),
        }
        if extra:
            msg.update(extra)
        try:
            self.redis.publish(channel, json.dumps(msg, ensure_ascii=False, default=str))
            logger.info(f"Published {event_type} to {channel}")
        except Exception as e:
            logger.error(f"Failed to publish lifecycle event: {e}")

    # ── 主→子 指令发布 ───────────────────────────

    def publish_cmd(self, task_id: str, cmd: dict):
        """主Agent 向子Agent 下发指令。

        Args:
            task_id: 目标任务 ID
            cmd: {"type": "cmd_start|cmd_pause|cmd_resume|cmd_cancel", ...}
        """
        channel = f"agent:cmd:{task_id}"
        cmd.setdefault("task_id", task_id)
        cmd.setdefault("timestamp", _now())
        try:
            self.redis.publish(channel, json.dumps(cmd, ensure_ascii=False, default=str))
            logger.info(f"Published cmd to {channel}: {cmd.get('type')}")
        except Exception as e:
            logger.error(f"Failed to publish cmd to {channel}: {e}")


# ── Timer 源 ──


@dataclass
class TimerDef:
    name: str
    interval_seconds: float
    timer_type: str = "custom"
    context: dict = field(default_factory=dict)
    next_fire: float = 0.0  # monotonic 时间戳
    last_fire: float = 0.0


class TimerEventSource:
    """asyncio Timer 源 — pop_fired() 返回所有到期 Timer。

    每个 Timer 独立 asyncio.Task，到期后设置 fired 标记。
    RunLoop 通过 pop_fired() 收集并处理。
    """

    def __init__(self):
        self._timers: dict[str, TimerDef] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._fired: list[TimerFiredEvent] = []
        self._running = False

    def start(self):
        self._running = True
        logger.info(f"TimerEventSource started with {len(self._timers)} timers")

    async def stop(self):
        self._running = False
        for name, task in list(self._tasks.items()):
            task.cancel()
        for name in list(self._tasks.keys()):
            try:
                await self._tasks[name]
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._fired.clear()
        logger.info("TimerEventSource stopped")

    def register(self, timer: TimerDef):
        if timer.name in self._timers:
            self.cancel(timer.name)
        timer.next_fire = time.monotonic() + timer.interval_seconds
        self._timers[timer.name] = timer
        if self._running:
            self._tasks[timer.name] = asyncio.create_task(self._run_timer(timer))
        logger.info(f"Timer registered: {timer.name} (every {timer.interval_seconds}s)")

    def cancel(self, name: str):
        if name in self._tasks:
            self._tasks[name].cancel()
            del self._tasks[name]
        self._timers.pop(name, None)

    def list_timers(self) -> list[dict]:
        return [{"name": t.name, "interval": t.interval_seconds,
                 "type": t.timer_type, "next_fire": t.next_fire}
                for t in self._timers.values()]

    def pop_fired(self) -> list[TimerFiredEvent]:
        """返回所有到期 Timer 事件，并清空 _fired 列表。"""
        if not self._fired:
            return []
        fired = self._fired[:]
        self._fired.clear()
        return fired

    async def _run_timer(self, timer: TimerDef):
        """定时器协程 — 到期后追加到 _fired 列表。"""
        while self._running:
            try:
                await asyncio.sleep(timer.interval_seconds)
                timer.last_fire = time.monotonic()
                timer.next_fire = timer.last_fire + timer.interval_seconds
                self._fired.append(TimerFiredEvent(
                    timer_name=timer.name,
                    timer_type=timer.timer_type,
                    context=timer.context,
                ))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Timer {timer.name} error: {e}")


# ═══════════════════════════════════════════════════════════════
# EventBus (简化版 — 仅保留事件类型订阅)
# ═══════════════════════════════════════════════════════════════


class EventBus:
    """简化事件总线 — 仅保留事件类型订阅。

    Tick-Polling 模式下，不再通过 PriorityQueue 聚合事件。
    RunLoop 直接遍历各源的 drain/pop_nowait/pop_fired。
    此类的 subscribe/publish_to_subscribers 用于 Observer 等内部订阅。
    """

    def __init__(self):
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        self._running = True
        logger.info("EventBus started")

    def stop(self):
        self._running = False
        logger.info("EventBus stopped")

    async def subscribe(self, event_type: str) -> asyncio.Queue:
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[event_type].append(q)
        return q

    def publish_to_subscribers(self, event_type: str, data: Any):
        """推送给匹配的订阅者。"""
        subscribers = self._subscribers.get(event_type, [])
        for q in subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
