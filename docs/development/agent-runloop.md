# AgentRunLoop — 事件驱动架构

> 主 Agent 的事件循环、统一 Celery 调度、定时任务管理与多 Agent 部署 | 2026-06-16

---

## 一、设计目标

AgentRunLoop 是主 Agent 的**唯一事件循环**。灵感来自 iOS `CFRunLoop`——没有事件时休眠，有事件时唤醒，逐个处理。

### 核心原则

| 原则 | 说明 |
|------|------|
| **永不阻塞** | PlanGraph 执行作为独立协程，不在 RunLoop 内 await |
| **优先级队列** | iOS 用户消息 > 子Agent 完成 > 进度更新 > 定时任务 |
| **Fire-and-Subscribe** | 所有慢操作（IO/网络/LLM）分发到 Celery，立即返回 |
| **统一调度** | 所有 Tool（除纯内存操作外）统一走 Celery Worker 池 |

---

## 二、系统拓扑

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Redis (共享)                                  │
│  ├── Celery Broker (redis://localhost:6379/1)                       │
│  ├── agent:events:{agent_id}  — 每个 Agent 独立的 BRPOP 队列        │
│  └── agent:cmd:{agent_id}     — 每个 Agent 独立的 Pub/Sub 指令通道  │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
       ┌─────────────────────────┼─────────────────────────┐
       │                         │                         │
       ▼                         ▼                         ▼
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ agent-cv :8001   │  │ agent-nlp :8002  │  │ agent-001 :8000  │
│ ┌──────────────┐ │  │ ┌──────────────┐ │  │ ┌──────────────┐ │
│ │ AgentRunLoop │ │  │ │ AgentRunLoop │ │  │ │ AgentRunLoop │ │
│ │              │ │  │ │              │ │  │ │              │ │
│ │ PriorityQueue│ │  │ │ PriorityQueue│ │  │ │ PriorityQueue│ │
│ │  ├─ WS src   │ │  │ │  ├─ WS src   │ │  │ │  ├─ WS src   │ │
│ │  ├─ EventBus │ │  │ │  ├─ EventBus │ │  │ │  ├─ EventBus │ │
│ │  ├─ Redis src│ │  │ │  ├─ Redis src│ │  │ │  ├─ Redis src│ │
│ │  └─ Timer src│ │  │ │  └─ Timer src│ │  │ │  └─ Timer src│ │
│ │       │      │ │  │ │       │      │ │  │ │       │      │ │
│ │  PlanGraph   │ │  │ │  PlanGraph   │ │  │ │  PlanGraph   │ │
│ │  ToolRegistry│ │  │ │  ToolRegistry│ │  │ │  ToolRegistry│ │
│ └──────────────┘ │  │ └──────────────┘ │  │ └──────────────┘ │
│  独立 agent.db   │  │  独立 agent.db   │  │  独立 agent.db   │
│  独立 chroma/    │  │  独立 chroma/    │  │  独立 chroma/    │
└──────────────────┘  └──────────────────┘  └──────────────────┘
                                 │
       ┌─────────────────────────┴─────────────────────────┐
       │              Celery Worker 池 (共享)               │
       │                 ×N worker 进程                     │
       │                                                   │
       │  所有 Tool 执行: search / download / convert      │
       │  index / evaluate / rank / survey / web_search     │
       │  bash_exec / ...                                   │
       │                                                   │
       │  task 携带 agent_id → Reporter 推送到正确的        │
       │  agent:events:{agent_id} 队列                     │
       └───────────────────────────────────────────────────┘
```

---

## 三、AgentRunLoop 核心

### 3.1 事件优先级

```
prio=0  user_message        — iOS 用户消息，立即处理
        ios_tool_result     — iOS 工具结果
prio=1  celery_done         — 子Agent/Celery 任务完成
        celery_error        — 子Agent/Celery 任务失败
prio=2  celery_progress     — 进度更新（可批量）
        tool_start / tool_end — 工具状态变化
prio=3  timer_fired         — 定时任务触发
```

### 3.2 核心代码

```python
class AgentRunLoop:
    """单一线程事件循环。类似 iOS CFRunLoop。"""

    def __init__(self, plan_graph, ws_manager, redis_url, agent_id):
        self._queue = asyncio.PriorityQueue()  # (priority, seq, event)
        self._plan_graph = plan_graph
        self._ws_manager = ws_manager
        self._agent_id = agent_id
        self._redis_url = redis_url
        self._running = False
        self._seq = 0
        self._active_foreground_task = None  # 当前前台任务
        self._background_tasks = {}          # task_id → TaskHandle
        self._event_bus = EventBus()
        self._timer_manager = TimerManager(self._event_bus)

    async def run(self):
        """主循环入口。daemon.py 调用此方法。"""
        # 启动 4 个事件源（后台协程）
        asyncio.create_task(self._ws_source())
        asyncio.create_task(self._eventbus_source())
        asyncio.create_task(self._redis_source())
        asyncio.create_task(self._timer_source())

        # 注册系统固定 Timer
        self._timer_manager.add_system_timer("health_check", 1200, recurring=True)
        self._timer_manager.add_system_timer("cleanup_logs", 86400, recurring=True)

        self._running = True
        while self._running:
            priority, seq, event = await self._queue.get()
            await self._dispatch(event)

    async def _dispatch(self, event):
        """所有事件统一分发给 PlanGraph。"""
        etype = event["type"]

        # ── 中断前台任务（如果用户发新消息）──
        if etype == "user_message" and self._active_foreground_task:
            task_id = self._active_foreground_task
            self._background_tasks[task_id] = self._active_foreground_task
            self._active_foreground_task = None
            await self._ws_manager.broadcast(self._agent_id, event.get("session_id","main"), {
                "type": "message", "subType": "notification",
                "payload": {"title": "转入后台", "body": f"任务 {task_id} 已转入后台运行"},
            })

        match etype:
            case "user_message":
                asyncio.create_task(self._handle_user_message(event))
            case "ios_tool_result":
                asyncio.create_task(self._plan_graph.resume(event["data"]))
            case "celery_done":
                await self._on_task_done(event)
            case "celery_progress":
                await self._on_task_progress(event)
            case "celery_error":
                await self._on_task_error(event)
            case "timer_fired":
                asyncio.create_task(self._handle_timer(event))
            case "tool_start" | "tool_end":
                await self._ws_push_tool_status(event)
```

### 3.3 四个事件源

```python
async def _ws_source(self):
    """WebSocket 消息 → PriorityQueue(prio=0)"""
    # WS handler 收到消息后调用 self.push(0, event)

async def _eventbus_source(self):
    """EventBus 内部事件 → PriorityQueue(prio=1~2)"""
    # EventBus.on("*", lambda e: self.push(prio_from_type(e), e))

async def _redis_source(self):
    """Redis BRPOP agent:events:{agent_id} → PriorityQueue(prio=1~2)"""
    while self._running:
        _, raw = await self._redis.brpop(f"agent:events:{self._agent_id}")
        event = json.loads(raw)
        prio = 1 if event["type"] in ("celery_done","celery_error") else 2
        await self._queue.put((prio, self._next_seq(), event))

async def _timer_source(self):
    """Timer 触发 → PriorityQueue(prio=3)"""
    # TimerManager 内部触发时调用 self.push(3, event)

def push(self, priority: int, event: dict):
    """外部接口：向 RunLoop 投递事件。WS handler / EventBus / Timer 调用此方法。"""
    self._queue.put_nowait((priority, self._next_seq(), event))
```

---

## 四、统一 Celery 调度

### 4.1 工具分类

| 类型 | 条件 | 示例 | 执行方式 |
|------|------|------|---------|
| **纯内存** | 无 IO/网络，微秒完成 | `read_file`, `get_user_preference`, `list_timers`, `list_collections` | 直接调用，不阻塞 |
| **Celery Task** | 任何访问网络/磁盘/LLM/GPU 的操作 | 其余全部 50+ 工具 | `celery.send_task()` + subscribe 状态 |

### 4.2 Fire-and-Subscribe 模式

```python
# PlanGraph 调用工具的统一出口

async def execute_tool(tool_name: str, args: dict, foreground: bool = None):
    """执行工具 — fire-and-subscribe 模式。"""

    # 1. 自动判定前台/后台
    if foreground is None:
        foreground = _auto_decide_mode(tool_name, args)

    # 2. 纯内存工具直接执行
    if tool_name in MEMORY_ONLY_TOOLS:
        return await _call_direct(tool_name, args)

    # 3. Celery 工具 — fire
    task = celery_app.send_task(
        f"paper_search.agent.celery_tasks.{tool_name}_task",
        kwargs={"agent_id": self.agent_id, "session_id": self.session_id, **args},
    )

    # 4. Subscribe 状态
    task_id = task.id
    if foreground:
        self._active_foreground_task = task_id
    else:
        self._background_tasks[task_id] = task

    # 5. 立即返回（不等待 Celery 完成）
    # PlanGraph 继续处理下一个 RunLoop 事件
    return {"status": "dispatched", "task_id": task_id, "mode": "foreground" if foreground else "background"}
```

### 4.3 前台 → 后台自动切换

```
条件: 用户发新消息 && 当前有前台任务在跑
动作:
  1. 前台任务标记为 background
  2. iOS 收到 notification: "XX 任务已转入后台运行"
  3. 新消息立即处理
  4. 后台任务完成后，iOS 收到 notification: "XX 任务完成"
```

### 4.4 前台/后台判定规则

```python
FOREGROUND_THRESHOLDS = {
    "search_papers":     ("max_results", 10),     # ≤10 篇 → 前台
    "evaluate_papers":   ("count", 5),            # ≤5 篇 → 前台
    "download_paper":    ("count", 1),            # ≤1 篇 → 前台
    "web_search":        ("count", 5),            # ≤5 条 → 前台
}

# 用户可通过自然语言覆盖:
# "后台下载这 50 篇" → 强制后台
# "等等，先查这个"   → 强制前台
# "不用等了"         → 当前前台任务转入后台
```

---

## 五、Timer 管理

### 5.1 Timer 类型

| 类型 | 创建者 | 生命周期 | 示例 |
|------|--------|---------|------|
| **系统固定** | daemon 启动时注册 | agent 运行期间 | `health_check` (每 20min), `cleanup_logs` (每天) |
| **LLM 动态** | PlanGraph 调 `create_timer` tool | 可被 cancel | 用户说"每周一搜一下" → LLM 创建 weekly timer |
| **用户手动** | iOS 发送订阅 / CLI `timer` 命令 | 可被 cancel/修改 | 用户通过 App 设置研究方向订阅 |

### 5.2 Timer Tool（注册在 ToolRegistry）

```
create_timer:
  name: str              # 唯一标识
  interval: str          # "30min" / "1h" / "24h" / "monday 9:00"
  action: str            # 触发时的 prompt 描述
  recurring: bool = True
  → {"timer_id": "..."}

cancel_timer:
  name: str
  → {"cancelled": True}

list_timers:
  (无参数)
  → {"timers": [{"name":"...", "interval":"...", "recurring":true}, ...]}

fire_timer:
  name: str              # 手动触发（调试/运维用）
  → 立即插入 timer_fired 事件到 RunLoop
```

### 5.3 Timer 触发流程

```
Timer 到期
  │
  ▼
TimerManager._on_fire(name)
  │
  ▼
EventBus.emit("timer_fired", {"name": name, "action": "...", "type": "system|llm|user"})
  │
  ▼
RunLoop 收到 (prio=3)
  │
  ▼
PlanGraph.dispatch(timer_fired)
  │
  ├── 系统 Timer: health_check → 执行检查 → 异常则通知 iOS
  ├── LLM Timer: "每周一搜 adversarial attack"
  │     → PlanGraph 像处理用户消息一样:
  │        search_papers("adversarial attack", year_from=2025)
  │        → evaluate → 推送 iOS
  └── 用户 Timer: 同 LLM Timer，用户通过 iOS 或 CLI 设置
```

---

## 六、EventBus vs Redis BRPOP

### 6.1 分工

| 维度 | EventBus | Redis BRPOP |
|------|----------|-------------|
| **通信范围** | 同一进程内 | 跨进程 (Celery Worker ↔ Agent Daemon) |
| **实现** | `asyncio.Queue` | `redis.lpush` / `redis.brpop` |
| **延迟** | 纳秒级 | 毫秒级 |
| **持久化** | ❌ 进程重启丢失 | ✅ Redis AOF |
| **适用场景** | PlanGraph→工具状态→iOS 推送 | Celery Worker→Daemon 进度上报 |

### 6.2 最终汇入同一个 PriorityQueue

```python
# EventBus (进程内) — 直接 push
EventBus.on("tool_start", lambda e: runloop.push(2, e))

# Redis BRPOP (跨进程) — 先取出再 push
async def _redis_source():
    while True:
        _, raw = await redis.brpop(f"agent:events:{agent_id}")
        runloop.push(2, json.loads(raw))
```

### 6.3 不冲突

两者处理**不同来源**的事件。EventBus 处理进程内事件（纳秒级），Redis 处理跨进程事件（毫秒级）。最终全部汇入同一个 PriorityQueue，RunLoop 统一分发。没有冗余，没有竞争。

---

## 七、后台任务可见性

### 7.1 iOS 聊天列表 + 进度卡片

```
┌─────────────────────────────────┐
│ 聊天列表                         │
│                                 │
│ ┌─────────────────────────────┐ │
│ │ 🏃 运行中 — 2 个任务         │ │
│ │                             │ │
│ │ ┌─────────────────────────┐ │ │
│ │ │ 📄 入库 Transformer      │ │ │
│ │ │ ████████░░░░ 68% (34/50)│ │ │
│ │ │ 下载中...               │ │ │
│ │ └─────────────────────────┘ │ │
│ │ ┌─────────────────────────┐ │ │
│ │ │ 🔍 搜索 adversarial     │ │ │
│ │ │ ████████████ 完成      │ │ │
│ │ │ 找到 23 篇，12 篇高相关 │ │ │
│ │ └─────────────────────────┘ │ │
│ └─────────────────────────────┘ │
│                                 │
│ ┌─────────────────────────────┐ │
│ │ 💬 主会话 — 最后消息: ...    │ │
│ └─────────────────────────────┘ │
└─────────────────────────────────┘
```

### 7.2 WS 消息协议扩充

为支持后台任务可见性，在现有协议上新增：

```json
// task_start — 新任务开始
{
  "type": "message", "subType": "task_update",
  "payload": {
    "task_id": "task-20260616-001",
    "status": "started",
    "mode": "foreground|background",
    "name": "入库 Transformer 安全方向",
    "total_stages": 7,
    "current_stage": "search"
  }
}

// task_progress — 进度更新
{
  "type": "message", "subType": "task_update",
  "payload": {
    "task_id": "task-20260616-001",
    "status": "running",
    "stage": "download",
    "current": 34,
    "total": 50
  }
}

// task_done — 任务完成
{
  "type": "message", "subType": "task_update",
  "payload": {
    "task_id": "task-20260616-001",
    "status": "done",
    "result": {"total": 50, "downloaded": 48, "failed": 2}
  }
}
```

---

## 八、多 Agent 部署

### 8.1 启动命令

```bash
# 默认 Agent
python scripts/start.py --port 8000 --agent-id agent-001

# CV 论文研究 Agent
python scripts/start.py --port 8001 --agent-id agent-cv \
    --db ~/.paper_search/agent-cv.db

# NLP 论文研究 Agent
python scripts/start.py --port 8002 --agent-id agent-nlp \
    --db ~/.paper_search/agent-nlp.db
```

### 8.2 隔离边界

| 资源 | 隔离方式 |
|------|---------|
| WebSocket | 不同端口 |
| SQLite | 不同 db 文件 |
| ChromaDB | 共享 ChromaDB（按 agent_id 过滤 collection） |
| Redis 事件 | 不同队列 `agent:events:{agent_id}` |
| Celery Task | task 携带 `agent_id`，Reporter 路由到正确队列 |
| PlanGraph | 独立 thread_id `{agent_id}-plan` |

### 8.3 共享资源

| 资源 | 共享方式 |
|------|---------|
| Redis | 单实例，不同 DB/队列前缀 |
| Celery Worker 池 | 共享，task 按 agent_id 路由 |
| ChromaDB 向量存储 | 共享（collection 名带 agent_id 前缀或统一管理） |
| 论文文件 (~/papers/) | 共享（多 Agent 可看到相同的 PDF 库） |

---

## 九、一期不做的

| 项目 | 原因 |
|------|------|
| 多 session（同一 Agent 内） | 多 Agent 已经天然隔离，单 Agent 单 session 够用 |
| iOS 任务管理 Tab | Phase 2，先用聊天列表卡片 |
| Agent 间通信 | 每个 Agent 独立运作，无跨 Agent 需求 |
| 动态 Worker 扩缩容 | Celery 默认 `--concurrency=4` 固定 |

---

> 版本: v1.0 | 2026-06-16
