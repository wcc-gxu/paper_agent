# Phase 2 实施计划 — LangGraph 核心

> 基于 24 个结构化问题的回答 | 2026-06-18

---

## 架构决策摘要

| # | 决策 |
|---|------|
| 1 | 一个 PlanGraph 驱动所有子 Agent（不各自独立） |
| 2 | 增加 `execute_graph.py` 调度层，解耦 PlanGraph 与具体子 Agent |
| 3 | 移除 `auto_pipeline.py`（逻辑已迁移到 ingest_graph） |
| 4 | Plan Graph 增加**权限统一确认**节点（在用户批准 plan 之后、执行之前） |
| 5 | daemon.py 缺少 RunLoop + 4 事件源（见上方复盘） |
| 6 | PlanGraph 等待用户期间**阻塞等待**（LangGraph interrupt） |
| 7 | 只做单一 Agent 实例 |
| 8 | **Daemon 和 FastAPI 独立进程**，通过 Redis 通信 |
| 9 | 需要统一的 `event_bus.py` |
| 10 | 跨进程用 Redis BRPOP，同进程用 EventBus（见上方对比分析） |
| 11 | 崩溃恢复依赖 LangGraph SqliteSaver checkpoint，事件重放由子 Agent 负责 |
| 12 | 生产环境用 **SqliteSaver** |
| 13 | 崩溃后通知用户，由用户决定是否恢复 |
| 14 | 统一使用 LangGraph SqliteSaver，PlanGraph 和子 Agent 独立存储 |
| 15 | `verifier.py` **完全重写** |
| 16 | Verifier 作为 IngestAgent 的**可选独立 stage**（rank 之后，survey 之前） |
| 17 | 子 Agent **统一改为 Celery 调度** |
| 18 | 先不做多用户 |
| 18+ | 主 Agent 通过 **Redis Pub/Sub** 监听子 Agent 实时 report；子 Agent 独立日志，可按 task_id 区分条目 |
| 19 | **全部 6 个子 Agent** 实现 |
| 20 | 实现 `ws_handler.py` |
| 21 | `task_event_adapter.py` 按新设计**重写** |
| 22 | Phase 1 的 **6 个 Bug 全部修复** |
| 23 | 不做可视化验证和单元测试 |
| 24 | 全部子 Agent 开发，不延期 |

---

## 子 Agent → 主 Agent 实时通信（Redis Pub/Sub）

```
主 Agent (PlanGraph)                    子 Agent (Celery Worker)
      │                                        │
      ├─ SUBSCRIBE agent:reports:{task_id} ──►│
      │                                        ├─ 执行阶段
      │                                        ├─ PUBLISH agent:reports:{task_id}
      │  ◄── {"stage":"download","paper":5,    │   每篇论文处理完即发布
      │        "total":50,"status":"done"}      │
      │                                        ├─ PUBLISH agent:reports:{task_id}
      │  ◄── {"stage":"index","chunks":12}     │
      │                                        ├─ 完成
      │                                        ├─ PUBLISH agent:reports:{task_id}
      │  ◄── {"type":"celery_done","result":{}}│
      │                                        │
      ├─ UNSUBSCRIBE                          │
```

**子 Agent 日志隔离**：
```
~/.paper_search/logs/
├── agent.log                    # 全局日志
└── sub_agents/
    ├── ingest/
    │   ├── task-20260618-001.jsonl    # 按 task_id 分文件
    │   └── task-20260618-002.jsonl
    ├── citation_chase/
    │   └── task-20260618-003.jsonl
    └── ...
```

每条日志带 `task_id` + `agent_type` + `timestamp`，可按任务过滤。

---

## 实施步骤

### Step 0: Phase 1 Bug 修复（先决条件）

| Bug | 文件 | 修复 |
|-----|------|------|
| agent_loop cancel | agent_loop.py | 取消信号传播 |
| keyword pass | 搜索链路 | 关键词传递修复 |
| verifier 全文 | verifier.py | PDF 全文接入（重写后自然解决） |
| auto_pipeline 日期 | 已移除 | N/A |
| timeout | LLM 调用 | 超时处理 + 重试 |
| knowledge 截断 | knowledge.py | Cross-Encoder 截断修复 |

### Step 1: 核心基础设施（第 1 周）

#### 1.1 `event_bus.py` — 统一事件总线

```
src/paper_search/agent/event_bus.py
├── EventBus (asyncio.Queue)           # 同进程事件
│   ├── push(event, priority)
│   └── pop() → (priority, event)
├── RedisEventSource                   # BRPOP agent:events
│   └── 投递到 EventBus (prio=1~2)
├── TimerEventSource                   # asyncio timer
│   └── 投递到 EventBus (prio=3)
└── 事件类型枚举:
    - user_message (prio=0)
    - celery_done / celery_error (prio=1)
    - celery_progress / tool_start / tool_end (prio=2)
    - timer_fired (prio=3)
```

#### 1.2 `daemon.py` → 增加 AgentRunLoop

在现有 `AgentBootstrap` 基础上增加：

```python
class AgentRunLoop:
    """事件驱动主循环"""
    def __init__(self, graph, event_bus, ws_manager):
        self._queue = asyncio.PriorityQueue()
        self._sources = [
            WebSocketSource(ws_manager),    # prio=0
            RedisEventSource(redis_url),    # prio=1~2
            TimerEventSource(),             # prio=3
        ]

    async def run(self):
        # 启动所有事件源协程
        # while running: queue.get() → graph.dispatch(event)
```

`daemon.py` 的 `main()` 改为启动 `AgentRunLoop.run()` 而非直接返回。

#### 1.3 `celery_tasks.py` + `reporter.py` → 保持并增强

现有 `celery_tasks.py` 的 4 个 task（download / convert / index / survey）保持。`reporter.py` 保持 Redis LPUSH 模式。增加：
- `search_task` — 搜索也走 Celery（统一调度）
- `evaluate_task` — LLM 评估走 Celery
- `rank_task` — 期刊评级走 Celery

---

### Step 2: PlanGraph 重构（第 1-2 周）

#### 2.1 增加权限确认节点

在 `await_approval` 和 `execute_plan` 之间插入新节点：

```
generate_plan → await_approval → [NEW] await_permissions → execute_plan
                                     ↑
                              检查所有子 Agent 需要的权限
                              一次性生成权限清单 → review(permissions)
                              用户确认 → 进入 execute_plan
```

新增 `review(permissions)` 信封：
```json
{
  "type": "review",
  "subType": "permissions",
  "payload": {
    "taskId": "task-xxx",
    "permissions": [
      {"tool": "search_papers", "scope": "arxiv,semantic_scholar", "maxResults": 50},
      {"tool": "download_paper", "scope": "50 papers", "estimateSize": "200MB"},
      {"tool": "ios_notification_local", "scope": "task_complete"}
    ]
  }
}
```

#### 2.2 `_execute_plan` → 委托 ExecuteGraph

当前 `_execute_plan` 直接组装 IngestAgent。改为：

```python
async def _execute_plan(self, state):
    from ..execute_graph import ExecuteGraph
    executor = ExecuteGraph(self._db, self._llm, self._tools, self._task_adapter)
    result = await executor.dispatch(task_id, plan, celery_app)
    return {"plan": {**plan, "execution_results": [result]}, "plan_status": "done"}
```

---

### Step 3: ExecuteGraph 调度层（第 2 周）

#### 3.1 `graphs/execute_graph.py` — 统一子 Agent 调度器

```python
class ExecuteGraph:
    """PlanGraph → 子 Agent 的统一调度层。

    职责:
      1. 根据 plan.sub_tasks 确定需要哪些子 Agent
      2. 按依赖顺序编排子 Agent
      3. 将每个子 Agent 的执行封装为 Celery Task
      4. 收集结果 → 回传 PlanGraph
    """

    SUB_AGENT_MAP = {
        "ingest": IngestAgent,
        "rad_query": RADQueryAgent,
        "clustering": ClusteringAgent,
        "citation_chase": CitationChaseAgent,
        "history": HistoryAgent,
        "translation": TranslationAgent,
    }

    async def dispatch(self, task_id, plan, celery_app) -> dict:
        """根据 plan 分发到对应子 Agent(s)。"""
        sub_tasks = plan.get("sub_tasks", [])
        results = []
        for st in sub_tasks:
            agent_type = st.get("agent", "ingest")
            agent_cls = self.SUB_AGENT_MAP[agent_type]
            # 通过 Celery 异步执行子 Agent
            result = await self._execute_via_celery(agent_cls, st, celery_app)
            results.append(result)
        return self._merge_results(results)
```

#### 3.2 子 Agent 的 Celery 封装

每个子 Agent 的 ExecuteGraph 作为一个 Celery Task：

```python
# celery_tasks.py 新增
@app.task(bind=True)
def sub_agent_task(self, agent_type: str, state: dict):
    """通用子 Agent 执行任务。"""
    agent = SUB_AGENT_REGISTRY[agent_type]()
    graph = agent.compile(checkpointer=SqliteSaver(...))
    return await graph.ainvoke(state)
```

---

### Step 4: 六个子 Agent 全部实现（第 2-3 周）

#### 4.1 IngestAgent（已有，增强）

在现有 `ingest_graph.py` 基础上：
- 增加可选的 **verify stage**（rank 之后、survey 之前）
- 改为通过 Celery Task 启动（而非 plan_graph 直接 await）
- 集成新的 `verifier.py`

#### 4.2 RADQueryAgent — 知识库问答

```
src/paper_search/agent/graphs/rad_query_graph.py

StateGraph (5 节点):
  parse → route → search → evaluate(refine loop) → format

功能: 用户在知识库中提问 → 向量检索 → Reranker → LLM 生成答案
```

#### 4.3 ClusteringAgent — 研究方向聚类

```
src/paper_search/agent/graphs/clustering_graph.py

StateGraph (5 节点):
  load → cluster → label → visualize → detect

功能: 对项目论文做 K-means/HDBSCAN → LLM 标签 → 新方向发现
```

#### 4.4 CitationChaseAgent — 引用追溯

```
src/paper_search/agent/graphs/citation_chase_graph.py

StateGraph (7 节点):
  resolve → check → fetch(evaluate parallel) → filter → ingest(parallel) → decide(loop) → summarize

功能: 从种子论文出发 → 引用网络追踪 → 相关论文入库
```

#### 4.5 HistoryAgent — 历史消息处理

```
src/paper_search/agent/graphs/history_graph.py

Plan + Execute 双图:
  Plan: analyze → generate_plan
  Execute: archive → merge → skip → notify

功能: Agent 重启后处理未读消息 → 去重 → 归档 → 生成待办
```

#### 4.6 TranslationAgent — 术语翻译

```
src/paper_search/agent/graphs/translation_graph.py

无 Graph（工具型 Agent）:
  build_glossary / translate_query / enrich_terminology

功能: 维护中英学术术语库 → 查询翻译 → 从论文提取新术语
```

---

### Step 5: Verifier 重写（第 3 周）

#### 5.1 `verifier.py` — 完全重写

```python
class CitationVerifier:
    """引用幻觉防控 — 三步严格校验。

    1. 引用格式检查: [Author, Year] 或 [N] 格式
    2. 数据库匹配: 在 SQLite 中查找匹配论文
    3. 事实校验: 声明中的方法/数据集/指标与原文匹配
    """

    async def verify_claim(self, claim: str, paper_id: str) -> Verdict:
        """验证单条声明。"""

    async def verify_survey(self, survey_path: str, project_id: str) -> VerifyReport:
        """验证整篇综述的所有引用。"""
```

#### 5.2 集成到 IngestAgent

```
search → evaluate → download → convert → index → rank → [verify] → survey
                                                           ↑
                                                     可选 stage
                                                   用户可在 plan 中开启/关闭
```

---

### Step 6: WebSocket 集成（第 3 周）

#### 6.1 `ws_handler.py` — WebSocket 事件循环

```python
# src/paper_search/api/ws_handler.py

class WSHandler:
    """WebSocket 连接管理 + 会话持久化。

    职责:
      1. 接收 iOS 消息 → 投递到 EventBus (prio=0)
      2. 消费 EventBus 输出 → 发送 WS 消息到 iOS
      3. 会话断开时缓存事件到 Redis，重连时回放
    """
```

#### 6.2 `task_event_adapter.py` — 重写

按新架构重写：不再持有 `send_fn` 回调，改为通过 EventBus 发布事件，由 `ws_handler` 消费并发送。

```python
class TaskEventAdapter:
    """将任务生命周期事件发布到 EventBus。"""
    def __init__(self, event_bus: EventBus):
        self._bus = event_bus

    async def on_task_started(self, task_id, ...):
        await self._bus.push(TaskEvent(type="started", ...), priority=1)
```

---

### Step 7: FastAPI 进程（第 3-4 周）

#### 7.1 `api/app.py` — FastAPI 应用

独立进程，通过 Redis 与 Daemon 通信：

```python
# 启动方式（两个终端）:
# 终端1: python -m paper_search.agent.daemon
# 终端2: uvicorn paper_search.api.app:app --port 8000

# app.py
from fastapi import FastAPI
app = FastAPI()

# REST 端点
@app.post("/api/tasks")
@app.get("/api/tasks/{id}")
@app.get("/api/papers")

# WebSocket 端点
@app.websocket("/ws/chat/{agent_id}/{session_id}")
async def ws_chat(websocket, agent_id, session_id):
    handler = WSHandler(redis_url=REDIS_URL)
    await handler.handle(websocket, agent_id, session_id)
```

#### 7.2 Daemon ↔ FastAPI 通信

```
FastAPI (WS)                    Daemon (AgentRunLoop)
    │                                │
    ├─ 收到用户消息                  │
    ├─ LPUSH redis:cmd:{agent_id} ──►│
    │                                ├─ BRPOP 消费
    │                                ├─ PlanGraph.dispatch()
    │                                ├─ 结果 LPUSH redis:events:{agent_id}
    │◄── BRPOP 消费 ────────────────┤
    │                                │
    ├─ WS send 给 iOS                │
```

---

## 文件变更总览

```
[重写]
  src/paper_search/agent/daemon.py         # +AgentRunLoop +4 事件源
  src/paper_search/agent/verifier.py       # 全新三步校验
  src/paper_search/agent/task_event_adapter.py  # 改为 EventBus 发布

[新增]
  src/paper_search/agent/event_bus.py      # 统一事件总线
  src/paper_search/agent/graphs/execute_graph.py  # 子 Agent 调度层
  src/paper_search/agent/graphs/rad_query_graph.py
  src/paper_search/agent/graphs/clustering_graph.py
  src/paper_search/agent/graphs/citation_chase_graph.py
  src/paper_search/agent/graphs/history_graph.py
  src/paper_search/agent/graphs/translation_graph.py
  src/paper_search/api/__init__.py
  src/paper_search/api/app.py              # FastAPI
  src/paper_search/api/ws_handler.py       # WebSocket
  src/paper_search/api/routes.py           # REST
  src/paper_search/api/auth.py             # API Key
  src/paper_search/api/middleware.py       # 速率限制

[增强]
  src/paper_search/agent/graphs/plan_graph.py    # +await_permissions 节点
  src/paper_search/agent/graphs/ingest_graph.py  # +verify stage (可选)
  src/paper_search/agent/celery_tasks.py         # +search/evaluate/rank/sub_agent tasks
  src/paper_search/agent/reporter.py             # 保持不变
  src/paper_search/agent/sub_agent.py            # PipelineRunner → Celery 封装

[移除]
  src/paper_search/agent/auto_pipeline.py        # 已不存在，确认移除引用
```

---

## 时间估算

| 周次 | 内容 |
|------|------|
| Week 1 | Step 0 (Bug修复) + Step 1 (event_bus + daemon RunLoop) + Step 2 (PlanGraph 权限节点) |
| Week 2 | Step 3 (ExecuteGraph 调度层) + Step 4 开始 (IngestAgent增强 + 2个子Agent) |
| Week 3 | Step 4 继续 (剩余3个子Agent) + Step 5 (verifier 重写) |
| Week 4 | Step 6 (ws_handler + task_event_adapter重写) + Step 7 (FastAPI 进程) + 联调 |

---

> 版本: v1.0 | 基于 24 个结构化问题的回答 | 2026-06-18
