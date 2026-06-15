# Paper Agent v3 — 技术架构与实施方案

> 从 Claude Code Harness → 自研 Agent 系统的完整演化 | 2026-06-14

---

## 1. 系统拓扑

```
                         ┌──────────────┐
                         │   iOS 客户端  │
                         │  APNs 推送    │
                         └──────┬───────┘
                                │ WebSocket
                         ┌──────┴───────┐
                         │   FastAPI     │
                         │  REST + WS    │
                         └──────┬───────┘
                                │
              ┌─────────────────┴──────────────────┐
              │         Agent 守护进程 (主线程)      │
              │                                     │
              │  ┌──────────────────────────────┐   │
              │  │  LangGraph 双图引擎           │   │
              │  │  Plan Graph + Execute Graph  │   │
              │  └──────────────────────────────┘   │
              │  ┌──────────────────────────────┐   │
              │  │  AgentRegistry (7 种子Agent)   │   │
              │  │  IngestAgent / RADQueryAgent  │   │
              │  │  ClusteringAgent /            │   │
              │  │  CitationChaseAgent /         │   │
              │  │  HistoryAgent /               │   │
              │  │  TranslationAgent             │   │
              │  └──────────────────────────────┘   │
              │  ┌──────────────────────────────┐   │
              │  │  TaskLogger (JSON 日志)       │   │
              │  │  task独立日志 + agent全局日志  │   │
              │  └──────────────────────────────┘   │
              │  ┌──────────────────────────────┐   │
              │  │  ToolRegistry (LangChain)     │   │
              │  │  Server Tools + iOS Tools     │   │
              │  └──────────────────────────────┘   │
              │  ┌──────────────────────────────┐   │
              │  │  Memory System (MemGPT)       │   │
              │  │  4 层记忆 + RAG               │   │
              │  └──────────────────────────────┘   │
              │  ┌──────────────────────────────┐   │
              │  │  Event Bus (Redis BRPOP)      │   │
              │  └──────────────────────────────┘   │
              └─────────────┬────────────────────┘
                            │
         ┌──────────────────┼──────────────────┐
         │                  │                  │
  ┌──────┴──────┐   ┌──────┴──────┐   ┌──────┴──────┐
  │   Redis     │   │   Celery    │   │   Storage   │
  │  事件队列   │   │  Worker     │   │  SQLite     │
  │  Broker     │   │  长任务执行  │   │  ChromaDB   │
  │  Beat定时   │   │             │   │  PDF/MD文件 │
  └─────────────┘   └──────┬──────┘   │  JSON 日志  │
                           │          │  (tasks/)   │
                    ┌──────┴──────┐   └─────────────┘
                    │  Providers  │
                    │  S2(P0) +  │
                    │  6降级来源  │
                    │  Engine     │
                    └─────────────┘
```

---

## 2. Agent 主线程事件循环

```
┌─────────────────────────────────────────────────┐
│            Agent Main Thread (asyncio)           │
│                                                 │
│   while True:                                   │
│     event = await redis.brpop("agent:events")   │
│                                                 │
│     match event.type:                           │
│       "user_message"    → LangGraph.astream()   │
│       "ios_tool_result" → LangGraph.resume()    │
│       "celery_progress" →                        │
│         level=normal → 缓存, 更新 iOS status    │
│         level=high   → 立即喂 LLM               │
│       "celery_done"    → LangGraph.resume()     │
│       "celery_error"   → LangGraph.resume()     │
│       "task_progress"  → 更新 task 日志 →       │
│         iOS 可通过 REST 查询最新进度            │
│       "subscription"   → LLM 评估 → iOS 推送    │
│       "trending"       → LLM 评估 → iOS 推送    │
│       "health_check"   → 执行检查 → 写日志      │
│                                                 │
│     → 每次 LLM 返回 → 更新会话 → 通知 iOS       │
└─────────────────────────────────────────────────┘
```

---

## 3. LangGraph 双图结构

### 3.1 Plan Graph（顶层）

```python
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

plan_graph = StateGraph(ResearchState)

plan_graph.add_node("parse_intent", parse_intent_node)      # LLM
plan_graph.add_node("clarify", clarify_node)                 # LLM
plan_graph.add_node("await_clarify", interrupt_node)         # 暂停等用户
plan_graph.add_node("generate_plan", generate_plan_node)     # LLM
plan_graph.add_node("await_approval", interrupt_node)        # 暂停等确认
plan_graph.add_node("execute_plan", execute_subgraph)        # Execute Graph

plan_graph.add_edge(START, "parse_intent")
plan_graph.add_conditional_edges("parse_intent", needs_clarify, {
    "yes": "clarify",
    "no": "generate_plan"
})
plan_graph.add_edge("clarify", "await_clarify")
plan_graph.add_edge("await_clarify", "generate_plan")
plan_graph.add_edge("generate_plan", "await_approval")
plan_graph.add_conditional_edges("await_approval", user_approved, {
    "yes": "execute_plan",
    "no": END
})
plan_graph.add_node("overall_evaluate", overall_evaluate_node)  # LLM
plan_graph.add_edge("execute_plan", "overall_evaluate")
plan_graph.add_conditional_edges("overall_evaluate", decide_overall, {
    "satisfied": END,
    "adjust": "generate_plan",        # ← 策略调整循环
})
```

### 3.2 Execute Graph — 每个子 Agent 独立定义

不再使用单一 Execute Graph。每个子 Agent 有自己的 StateGraph：

| Sub Agent | Graph 类型 | 节点数 | 特点 |
|-----------|-----------|--------|------|
| **IngestAgent** | 线性 Execute Graph | 7 节点 | search→evaluate→download→convert→index→rank→survey，无条件分支，每阶段自动 checkpoint |
| **RADQueryAgent** | 动态 Execute Graph | 5 节点 | parse→route→search→evaluate(refine loop)→format，条件分支+迭代循环 |
| **ClusteringAgent** | 线性 Execute Graph | 5 节点 | load→cluster→label→visualize→detect，无条件分支 |
| **CitationChaseAgent** | 动态 Execute Graph | 7 节点 | resolve→check→fetch(evaluate parallel)→filter→ingest(parallel)→decide(loop)→summarize |
| **HistoryAgent** | Plan Graph + Execute Graph | 2+4 节点 | Plan: analyze→generate_plan；Execute: archive→merge→skip→notify |
| **TranslationAgent** | 无 Graph | — | 工具型 Agent，直接调用：build_glossary / translate_query / enrich_terminology |

### 3.3 State 定义 — 每个 Agent 独立 State

```python
# 主 Agent State
class MainAgentState(TypedDict):
    messages: Annotated[list, add_messages]
    session_id: str
    active_agent_ids: list[str]
    plan: dict | None
    plan_status: str            # "pending" | "awaiting_approval" | "executing" | "done" | "needs_adjustment"
    ios_tools: list[dict]
    ios_connected: bool
    short_term_token_count: int
    compression_needed: bool

# IngestAgent State
class IngestState(TypedDict):
    project_id: str
    current_stage: str          # search|evaluate|download|convert|index|rank|survey
    stage_index: int
    celery_task_ids: dict
    papers: list[PaperStatus]   # 每篇论文的处理状态
    existing_papers: list[str]  # 已入库论文（增量模式）

# RADQueryAgent State
class QueryState(TypedDict):
    query_intent: dict
    target_collections: list[str]
    retrieval_rounds: int
    found_chunks: list[dict]
    is_complete: bool
```

---

## 4. 事件总线设计

### 4.1 事件类型

```python
# Agent 内部消费的事件
INTERNAL_EVENTS = {
    "user_message":      "iOS 用户发来新消息",
    "ios_tool_result":   "iOS 执行工具完成",
    "celery_progress":   "Celery Worker report() 进度",
    "celery_done":       "Celery 任务完成",
    "celery_error":      "Celery 任务失败",
    "task_progress":     "子Agent 写 JSON 日志后的进度通知",
    "subscription":      "Celery Beat 订阅检查结果",
    "trending":          "Celery Beat 热点发现",
    "health_check":      "定时健康检查",
    "memory_compress":   "ShortTerm 达到阈值",
}
```

### 4.2 事件流

```
Celery Worker                  Redis                     Agent 主线程
    │                            │                          │
    ├─ report(normal, data) ──→ lpush("agent:events")      │
    │                            │                          │
    ├─ report(high, data) ────→ lpush("agent:events") ───→ brpop → 立即喂 LLM
    │                            │                          │
    ├─ report(done, result) ──→ lpush("agent:events") ───→ brpop → LangGraph.resume
    │                            │                          │
    │                            │                          │
iOS Client ──WS──→ FastAPI ──→ lpush("agent:events") ───→ brpop → LangGraph.astream

子Agent (asyncio 协程)           TaskLogger                  Agent 主线程
    │                            │                          │
    ├─ pipeline 阶段进度 ──────→ 写 task .jsonl 文件        │
    │                            │                          │
    ├─ 单篇 paper 操作 ────────→ 写 paper_progress 事件     │
    │                            │                          │
    ├─ 阶段完成 ──────────────→ 写 stage_done 事件          │
    │                            │                          │
    ├─ 全流程完成 ────────────→ 写 task_done 事件 ────────→ 可通知 Agent 主线程
    │                            │                          │
iOS Client ──REST──→ FastAPI ──→ 读 task .jsonl 文件 ─────→ 返回进度 JSON
```

---

## 5. 工具系统

### 5.1 ToolRegistry（唯一注册中心）

```
src/paper_search/agent/tool_registry.py
    │
    ├── 注册: @register_tool / registry.register_direct()
    ├── 查询: get() / get_by_category() / get_by_tag()
    ├── 导出: to_langchain() / to_anthropic() / to_mcp()
    │
    └── 每个工具标记:
        ├── location: "server" | "ios"
        ├── category: search | download | convert | index | analyze | export | manage | kb | subscription
        ├── is_idempotent: bool (重试安全)
        ├── is_long_running: bool (→ Celery)
        └── progress_report: bool (→ TaskLogger JSON 日志)
```

### 5.2 工具列表

```
Server 工具 (Agent 进程内或 Celery 执行):
  search_papers         → Engine.search()          [同步] [进度✓]
  download_paper        → Celery Task              [异步] [进度✓]
  convert_paper         → Celery Task              [异步] [进度✓]
  index_paper           → Celery Task              [异步] [进度✓]
  evaluate_papers       → LLMClientV2              [同步] [进度✓]
  rank_papers           → JournalRanker            [同步] [进度✓]
  generate_survey       → Celery Task              [异步] [进度✓]
  paper_export          → 格式化                   [同步]
  paper_status          → DB 查询                  [同步]
  paper_clean           → DB 操作                  [同步]
  batch_search          → Engine.batch_search()    [同步] [进度✓]
  citation_chase        → Semantic Scholar API     [同步]
  list_sources          → Engine.health_check()    [同步]
  extract_knowledge     → LLM + DB                 [异步] [进度✓]
  search_knowledge      → ChromaDB                 [同步]
  search_library        → ChromaDB                 [同步]
  read_paper            → 文件读取                 [同步]
  get_paper_abstract    → DB 查询                  [同步]
  search_memory         → ChromaDB conversations   [同步]
  list_collections      → ChromaDB                 [同步]
  get_user_preference   → MetaMemory               [同步]
  summarize_memory      → LLM 压缩                [同步]
  delete_memory         → ShortTerm 操作          [同步]
  extract_to_long_term  → LongTermMemory           [同步]
  tag_memory            → ShortTerm 操作          [同步]

iOS 工具 (客户端声明, 发 tool_use 给 iOS 执行):
  share_sheet, open_url, save_file, ...
  (每次用户消息携带当前可用列表)
```

---

## 6. 代码组织

```
src/paper_search/
├── agent/
│   ├── graphs/                 [NEW] LangGraph 图定义
│   │   ├── plan_graph.py
│   │   ├── execute_graph.py
│   │   └── supervisor.py       [STUB]
│   │
│   ├── daemon.py               [NEW] Agent 守护进程入口
│   ├── event_bus.py            [NEW] Redis 事件总线
│   ├── reporter.py             [NEW] Celery report() API
│   ├── celery_app.py           [NEW] Celery 配置
│   ├── celery_tasks.py         [NEW] Celery Task 定义
│   │
│   ├── task_logger.py           [NEW] TaskLogger — JSON 日志写入器
│   ├── sub_agent.py             [NEW] 子Agent 编排器 — PipelineRunner
│   │
│   ├── prompts.py              [NEW] 统一 Prompt 模板
│   ├── langchain_tools.py      [NEW] LangChain 工具注册
│   │
│   ├── tool_registry.py        [REFACTOR] 去 Singleton
│   ├── llm_client.py           [KEEP] V1, 向后兼容
│   ├── llm_client_v2.py        [KEEP] V2, 多供应商
│   │
│   ├── memory.py               [FIX] _maybe_compress + tiktoken
│   ├── prompt_optimizer.py     [REFACTOR] 统一 Plan 格式
│   ├── auto_pipeline.py        [REFACTOR] → Execute Graph 子图
│   ├── knowledge.py            [FIX] Cross-Encoder + 截断
│   ├── verifier.py             [FIX] PDF 全文接入
│   │
│   ├── db.py                   [KEEP] SQLite
│   ├── chroma_store.py         [KEEP] 双 Collection
│   ├── chunker.py              [KEEP]
│   ├── pdf_converter.py        [KEEP]
│   ├── journal_ranker.py       [KEEP]
│   ├── wiki_generator.py       [KEEP]
│   └── agent.py                → legacy_agent.py [ARCHIVE]
│
├── api/
│   ├── app.py                  [REWRITE] FastAPI + WS
│   ├── routes.py               [REWRITE] REST 端点
│   ├── ws_handler.py           [NEW] WebSocket 事件循环
│   ├── auth.py                 [NEW] API Key
│   └── middleware.py            [NEW] 速率限制 + 日志
│
├── mcp/
│   └── server.py               [REFACTOR] ToolRegistry 适配器
│
├── cli/                        [KEEP] 12 CLI, 不动
├── providers/                  [KEEP] 6 来源 + 预留扩展
├── downloaders/                [KEEP]
│
├── engine.py                   [KEEP]
├── models.py                   [EXTEND] ErrorResponse, WS 消息类型
└── config.py                   [EXTEND] Redis, Celery 配置
```

---

## 7. 实施阶段

### Phase 1: 基础重构（2-3 周）

| 任务 | 描述 |
|------|------|
| 1. 修复 6 Bug | agent_loop cancel / keyword pass / verifier 全文 / auto_pipeline 日期 / timeout / knowledge 截断 |
| 2. langchain_tools.py | 新建独立注册文件，引用 MCP 函数 |
| 3. prompts.py | 提取所有 System Prompt 统一管理 |
| 4. Plan 格式统一 | TaskPlan = GeneratedPlan |
| 5. ShortTerm 修复 | tiktoken + LLM 压缩 |
| 6. MCP Server 适配 | 改为 ToolRegistry → MCP 薄适配器 |

### Phase 2: LangGraph 核心（3-4 周）

| 任务 | 描述 |
|------|------|
| 7. daemon.py | Agent 守护进程入口 + Redis 事件循环 |
| 8. plan_graph.py | Plan Graph 实现 |
| 9. execute_graph.py | Execute Graph 实现 |
| 10. event_bus.py | Redis BRPOP 事件总线 |
| 11. MemorySaver 集成 | LangGraph checkpoint 替代自研 |
| 12. CitationVerifier 集成 | 接入 Execute Graph verify 阶段 |
| 13. AutoPipeline 改造 | 改为 Execute Graph 子图 |

### Phase 3: 异步任务 + API（2-3 周）

| 任务 | 描述 |
|------|------|
| 14. celery_app.py + tasks | Celery 配置 + 长任务定义 |
| 15. task_logger.py | TaskLogger — 统一 JSON 日志写入器，task 独立日志 + agent 全局日志 |
| 16. sub_agent.py | 子Agent 编排器 — asyncio 协程，编排入库全流程，进度回调注入 |
| 17. reporter.py | Celery report() API + JSON 日志写入 |
| 18. ws_handler.py | WebSocket 4 种事件 + 会话持久化 |
| 19. routes.py | REST 知识库端点 + task 日志查询端点 |
| 20. auth.py + middleware.py | API Key + 速率限制 |
| 21. iOS tool_use 链路 | 并发 + LLM 设超时 |
| 22. APNs 推送 | 离线通知 |

### Phase 4: Memory + 知识库增强（1-2 周）

| 任务 | 描述 |
|------|------|
| 23. Cross-Encoder Reranker | sentence-transformers |
| 24. MemGPT 多工具 Memory | summarize/delete/extract/search/tag |
| 25. Celery Beat 定时任务 | health_check / subscriptions / trending / cleanup |
| 26. System Prompt 精调 | 完全主动科研助理调教 |

### Phase 5: 测试 + 部署（1-2 周）

| 任务 | 描述 |
|------|------|
| 27. 单元测试 | TaskLogger / ToolRegistry / Memory / EventBus / Reporter / SubAgent |
| 28. 集成测试 | E2E 搜索→综述 全流程 (含 JSON 日志验证) |
| 29. Dockerfile + compose | agent + worker + redis |
| 30. 文档 | README + ARCHITECTURE + 进度日志 Schema |

---

## 8. 验证方式

| Phase | 验证 |
|-------|------|
| Phase 1 | pytest 通过；MCP Server 工具列表与 ToolRegistry 一致 |
| Phase 2 | LangGraph 图可视化；checkpoint 保存/恢复 |
| Phase 3 | WS 消息格式正确；Celery 任务消费正常；iOS 可连 |
| Phase 4 | Reranker 召回提升；定时任务自动执行 |
| Phase 5 | docker-compose up 一键运行；E2E 全流程通过 |

---

> 版本: v1.2 | 多Agent拆分、State分治、overall_evaluate循环、TranslationAgent
