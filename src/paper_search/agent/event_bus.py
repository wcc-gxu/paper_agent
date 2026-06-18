"""统一事件总线 — AgentRunLoop 的核心事件分发机制。

架构:
  同进程 (纳秒级):
    PlanGraph ↔ iOS 状态回调 ↔ 工具状态
    → EventBus (asyncio.PriorityQueue)

  跨进程 (毫秒级):
    Celery Worker → Reporter → Redis LPUSH → Daemon BRPOP
    → RedisEventSource 投递到 EventBus

  定时 (Timer):
    asyncio 定时器 → TimerEventSource 投递到 EventBus

  三者汇入同一个 PriorityQueue，RunLoop 按优先级统一消费。

优先级:
  prio=0  user_message / ios_tool_result     ← iOS 用户，立即处理
  prio=1  celery_done / celery_error         ← 子Agent 完成，尽快
  prio=2  celery_progress / tool_start/end   ← 进度状态，可批量
  prio=3  timer_fired                        ← 定时任务触发

使用方式:
    bus = EventBus()
    await bus.push(UserMessageEvent(...), priority=0)
    priority, seq, event = await bus.pop()
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
# 优先级
# ═══════════════════════════════════════════════════════════════


class Priority(IntEnum):
    USER = 0       # iOS 用户消息
    CELERY_DONE = 1  # Celery 任务完成/错误
    CELERY_PROGRESS = 2  # Celery 进度/工具状态
    TIMER = 3       # 定时触发


# ═══════════════════════════════════════════════════════════════
# 事件类型
# ═══════════════════════════════════════════════════════════════


class EventType:
    # ── 用户消息 (prio=0) ──
    USER_MESSAGE = "user_message"
    USER_CLARIFICATION = "user_clarification"   # 用户对澄清问题的回答
    USER_APPROVAL = "user_approval"             # 用户审批 plan/permissions
    IOS_TOOL_RESULT = "ios_tool_result"         # iOS 工具执行结果

    # ── Celery 结果 (prio=1) ──
    CELERY_DONE = "celery_done"
    CELERY_ERROR = "celery_error"

    # ── 进度 (prio=2) ──
    CELERY_PROGRESS = "celery_progress"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"

    # ── 定时 (prio=3) ──
    TIMER_FIRED = "timer_fired"

    # ── 系统 ──
    SYSTEM_SHUTDOWN = "system_shutdown"
    SYSTEM_HEALTH_CHECK = "system_health_check"


# ═══════════════════════════════════════════════════════════════
# 事件数据类
# ═══════════════════════════════════════════════════════════════


@dataclass
class Event:
    """事件基类。"""
    type: str
    priority: int = Priority.CELERY_PROGRESS
    seq: int = 0
    timestamp: float = field(default_factory=time.monotonic)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    # 路由信息
    agent_id: str = "agent-001"
    session_id: str = "main"


@dataclass
class UserMessageEvent(Event):
    """用户消息 — prio=0。"""
    content: str = ""
    raw_envelope: dict = field(default_factory=dict)

    def __post_init__(self):
        self.type = EventType.USER_MESSAGE
        self.priority = Priority.USER


@dataclass
class UserClarificationEvent(Event):
    """用户对澄清问题的回答。"""
    answers: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.type = EventType.USER_CLARIFICATION
        self.priority = Priority.USER


@dataclass
class UserApprovalEvent(Event):
    """用户审批 (plan / permissions)。"""
    confirmed: bool = False
    modifications: dict = field(default_factory=dict)

    def __post_init__(self):
        self.type = EventType.USER_APPROVAL
        self.priority = Priority.USER


@dataclass
class CeleryDoneEvent(Event):
    """Celery 任务完成 — prio=1。"""
    celery_task_id: str = ""
    agent_task_id: str = ""
    agent_type: str = ""       # ingest / citation_chase / ...
    result: dict = field(default_factory=dict)

    def __post_init__(self):
        self.type = EventType.CELERY_DONE
        self.priority = Priority.CELERY_DONE


@dataclass
class CeleryErrorEvent(Event):
    """Celery 任务失败 — prio=1。"""
    celery_task_id: str = ""
    agent_task_id: str = ""
    agent_type: str = ""
    error: str = ""

    def __post_init__(self):
        self.type = EventType.CELERY_ERROR
        self.priority = Priority.CELERY_DONE


@dataclass
class CeleryProgressEvent(Event):
    """Celery 进度 — prio=2。"""
    celery_task_id: str = ""
    agent_task_id: str = ""
    agent_type: str = ""
    stage: str = ""
    stage_index: int = 0
    total_stages: int = 0
    current: int = 0
    total: int = 0
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.type = EventType.CELERY_PROGRESS
        self.priority = Priority.CELERY_PROGRESS


@dataclass
class TimerFiredEvent(Event):
    """定时触发 — prio=3。"""
    timer_name: str = ""
    timer_type: str = ""  # health_check / subscription / cleanup
    context: dict = field(default_factory=dict)

    def __post_init__(self):
        self.type = EventType.TIMER_FIRED
        self.priority = Priority.TIMER


# ═══════════════════════════════════════════════════════════════
# EventBus
# ═══════════════════════════════════════════════════════════════


class EventBus:
    """统一事件总线 — 基于 asyncio.PriorityQueue。

    同进程内的事件发布/订阅。跨进程事件通过 RedisEventSource 流入。
    """

    def __init__(self, maxsize: int = 0):
        """
        Args:
            maxsize: 队列最大长度 (0 = 无限制)
        """
        self._queue: asyncio.PriorityQueue[tuple[int, int, Event]] = asyncio.PriorityQueue(maxsize=maxsize)
        self._seq = 0
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self):
        """启动 EventBus。"""
        self._running = True
        logger.info("EventBus started")

    async def stop(self):
        """停止 EventBus。"""
        self._running = False
        # 发送 shutdown 哨兵
        await self._queue.put((Priority.TIMER + 1, -1,
            Event(type=EventType.SYSTEM_SHUTDOWN, priority=Priority.TIMER + 1)))
        logger.info("EventBus stopped")

    async def push(self, event: Event, priority: Optional[int] = None) -> int:
        """发布事件到总线。

        Args:
            event: 事件实例
            priority: 覆盖事件默认优先级

        Returns:
            分配的序列号
        """
        prio = priority if priority is not None else event.priority
        self._seq += 1
        event.seq = self._seq
        await self._queue.put((prio, self._seq, event))
        return self._seq

    async def pop(self) -> tuple[int, int, Event]:
        """从总线取出下一个事件（阻塞等待）。

        Returns:
            (priority, seq, event)
        """
        return await self._queue.get()

    def pop_nowait(self) -> Optional[tuple[int, int, Event]]:
        """非阻塞取出事件。"""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def subscribe(self, event_type: str) -> asyncio.Queue:
        """订阅特定类型的事件。

        Args:
            event_type: 事件类型字符串

        Returns:
            一个 asyncio.Queue，订阅者从中读取匹配的事件
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[event_type].append(q)
        return q

    async def publish_to_subscribers(self, event: Event):
        """将事件推送给匹配的订阅者。"""
        subscribers = self._subscribers.get(event.type, [])
        for q in subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


# ═══════════════════════════════════════════════════════════════
# Redis 事件源 — 跨进程通信
# ═══════════════════════════════════════════════════════════════


class RedisEventSource:
    """Redis BRPOP 消费者 — 将 Celery Worker 的进度事件注入 EventBus。

    监听队列: agent:events:{agent_id}
    事件格式:
      {"type": "celery_progress", "task_id": "...", "level": "normal|high", "data": {...}}
      {"type": "celery_done", "task_id": "...", "result": {...}}
      {"type": "celery_error", "task_id": "...", "error": "..."}
    """

    def __init__(self, bus: EventBus, redis_url: str = "redis://localhost:6379/0",
                 agent_id: str = "agent-001"):
        self._bus = bus
        self._redis_url = redis_url
        self._agent_id = agent_id
        self._queue_key = f"agent:events:{agent_id}"
        self._redis = None
        self._task: Optional[asyncio.Task] = None

    @property
    def redis(self):
        if self._redis is None:
            import redis as _redis
            self._redis = _redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def start(self):
        """启动 Redis 监听协程。"""
        self._task = asyncio.create_task(self._listen())
        logger.info(f"RedisEventSource started on {self._queue_key}")

    async def stop(self):
        """停止监听。"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("RedisEventSource stopped")

    async def _listen(self):
        """BRPOP 循环 — 将 Redis 事件转换为 EventBus 事件。"""
        while self._bus.running:
            try:
                result = await asyncio.to_thread(
                    self.redis.brpop, self._queue_key, timeout=5
                )
                if result is None:
                    continue  # timeout, 继续循环

                _, raw = result
                data = json.loads(raw) if isinstance(raw, str) else raw
                event_type = data.get("type", "")
                task_id = data.get("task_id", "")
                agent_type = data.get("agent_type", "")

                if event_type == "celery_done":
                    await self._bus.push(CeleryDoneEvent(
                        celery_task_id=task_id,
                        agent_task_id=data.get("agent_task_id", task_id),
                        agent_type=agent_type,
                        result=data.get("result", {}),
                    ))
                elif event_type == "celery_error":
                    await self._bus.push(CeleryErrorEvent(
                        celery_task_id=task_id,
                        agent_task_id=data.get("agent_task_id", task_id),
                        agent_type=agent_type,
                        error=data.get("error", ""),
                    ))
                elif event_type == "celery_progress":
                    await self._bus.push(CeleryProgressEvent(
                        celery_task_id=task_id,
                        agent_task_id=data.get("agent_task_id", task_id),
                        agent_type=agent_type,
                        stage=data.get("stage", ""),
                        stage_index=data.get("stage_index", 0),
                        total_stages=data.get("total_stages", 0),
                        current=data.get("current", 0),
                        total=data.get("total", 0),
                        data=data.get("data", {}),
                    ))
                else:
                    logger.debug(f"RedisEventSource: unknown event type '{event_type}'")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"RedisEventSource error: {e}")
                await asyncio.sleep(1)


# ═══════════════════════════════════════════════════════════════
# 子 Agent 实时报告 — Redis Pub/Sub
# ═══════════════════════════════════════════════════════════════


class SubAgentReportListener:
    """Redis Pub/Sub 监听器 — 主 Agent 订阅子 Agent 的实时报告。

    频道: agent:reports:{task_id}
    消息格式:
      {"task_id": "...", "agent_type": "ingest", "stage": "download",
       "paper_index": 5, "paper_total": 50, "paper_id": "...",
       "status": "done", "timestamp": "2026-06-18T10:30:00Z"}

    用法:
      listener = SubAgentReportListener(redis_url, bus)
      await listener.subscribe("task-20260618-001")
      # ... 子 Agent 执行中，实时收到 report ...
      await listener.unsubscribe("task-20260618-001")
    """

    def __init__(self, bus: EventBus, redis_url: str = "redis://localhost:6379/0"):
        self._bus = bus
        self._redis_url = redis_url
        self._redis = None
        self._pubsub = None
        self._task: Optional[asyncio.Task] = None
        self._active_subscriptions: set[str] = set()

    @property
    def redis(self):
        if self._redis is None:
            import redis as _redis
            self._redis = _redis.from_url(self._redis_url, decode_responses=True)
        return self._redis

    async def start(self):
        """启动 Pub/Sub 监听循环。"""
        self._pubsub = self.redis.pubsub()
        self._task = asyncio.create_task(self._listen())
        logger.info("SubAgentReportListener started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            self._pubsub.close()
        logger.info("SubAgentReportListener stopped")

    async def subscribe(self, task_id: str):
        """订阅子 Agent 的任务报告频道。"""
        channel = f"agent:reports:{task_id}"
        if channel not in self._active_subscriptions:
            self._pubsub.subscribe(channel)
            self._active_subscriptions.add(channel)
            logger.info(f"Subscribed to {channel}")

    async def unsubscribe(self, task_id: str):
        """取消订阅。"""
        channel = f"agent:reports:{task_id}"
        if channel in self._active_subscriptions:
            self._pubsub.unsubscribe(channel)
            self._active_subscriptions.discard(channel)
            logger.info(f"Unsubscribed from {channel}")

    async def _listen(self):
        """监听 Pub/Sub 消息 → 投递到 EventBus。"""
        while self._bus.running:
            try:
                message = await asyncio.to_thread(
                    self._pubsub.get_message, timeout=5.0
                )
                if message is None:
                    await asyncio.sleep(0.1)
                    continue

                if message.get("type") != "message":
                    continue

                data_str = message.get("data", "{}")
                data = json.loads(data_str) if isinstance(data_str, str) else data_str

                # 转换为 CeleryProgressEvent 推入 EventBus
                await self._bus.push(CeleryProgressEvent(
                    celery_task_id=data.get("task_id", ""),
                    agent_task_id=data.get("task_id", ""),
                    agent_type=data.get("agent_type", ""),
                    stage=data.get("stage", ""),
                    stage_index=data.get("stage_index", 0),
                    total_stages=data.get("total_stages", 0),
                    current=data.get("paper_index", 0),
                    total=data.get("paper_total", 0),
                    data=data,
                ), priority=Priority.CELERY_PROGRESS)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SubAgentReportListener error: {e}")
                await asyncio.sleep(1)


# ═══════════════════════════════════════════════════════════════
# Timer 事件源
# ═══════════════════════════════════════════════════════════════


@dataclass
class TimerDef:
    """定时器定义。"""
    name: str
    interval_seconds: float
    timer_type: str = "custom"  # health_check / subscription / cleanup / custom
    context: dict = field(default_factory=dict)


class TimerEventSource:
    """asyncio 定时器 — 周期性向 EventBus 投递 timer_fired 事件。

    支持三种注册方式:
      - 系统固定: daemon 启动注册 (health_check, cleanup_logs)
      - LLM 动态: PlanGraph 调 create_timer tool
      - 用户手动: iOS 订阅
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._timers: dict[str, TimerDef] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self):
        """启动所有已注册的定时器。"""
        logger.info(f"TimerEventSource started with {len(self._timers)} timers")

    async def stop(self):
        """停止所有定时器。"""
        for name, task in list(self._tasks.items()):
            task.cancel()
        for name in list(self._tasks.keys()):
            try:
                await self._tasks[name]
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        logger.info("TimerEventSource stopped")

    def register(self, timer: TimerDef):
        """注册并启动定时器。"""
        if timer.name in self._timers:
            # 取消旧的
            self.cancel(timer.name)

        self._timers[timer.name] = timer
        self._tasks[timer.name] = asyncio.create_task(self._run_timer(timer))
        logger.info(f"Timer registered: {timer.name} (every {timer.interval_seconds}s, type={timer.timer_type})")

    def cancel(self, name: str):
        """取消定时器。"""
        if name in self._tasks:
            self._tasks[name].cancel()
            del self._tasks[name]
        self._timers.pop(name, None)
        logger.info(f"Timer cancelled: {name}")

    def list_timers(self) -> list[dict]:
        """列出所有活跃定时器。"""
        return [
            {"name": t.name, "interval_seconds": t.interval_seconds,
             "type": t.timer_type, "context": t.context}
            for t in self._timers.values()
        ]

    async def _run_timer(self, timer: TimerDef):
        """定时器运行循环。"""
        while self._bus.running:
            try:
                await asyncio.sleep(timer.interval_seconds)
                await self._bus.push(TimerFiredEvent(
                    timer_name=timer.name,
                    timer_type=timer.timer_type,
                    context=timer.context,
                ))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Timer {timer.name} error: {e}")
