# Paper Agent — 架构知识沉淀

> 来自 2026-06-14 ~ 2026-06-15 的完整架构讨论。适用于面试准备和技术决策参考。

---

## 1. LangChain 生态全景

LangChain 不是单一库，是一个生态家族：

```
LangChain 生态
│
├── langchain-core        基础抽象层
│   ├── BaseMessage       统一消息格式 (HumanMessage, AIMessage, ToolMessage)
│   ├── BaseTool          工具抽象 (name, description, args_schema)
│   ├── BaseChatModel     LLM 抽象 (统一 OpenAI/Anthropic/火山 的调用)
│   ├── Runnable          可组合执行单元 (chain | prompt | model | parser)
│   └── Callbacks         事件回调系统 (on_llm_start, on_tool_end, etc.)
│
├── langgraph             状态图引擎
│   ├── StateGraph        节点+条件边的有状态工作流
│   ├── MessageGraph      消息列表专用图 (比 StateGraph 更轻)
│   ├── interrupt()       人机交互暂停点 (LangGraph 核心特性)
│   ├── Send API          多 Agent 并行扇出
│   ├── MemorySaver       checkpoint 持久化 (SQLite/Postgres)
│   └── Command           从 interrupt 点恢复 + 修改 state
│
├── langchain-community   社区集成 (60+ LLM, 30+ VectorStore, 20+ Tool)
│
├── langchain             高级抽象层 (Chain, Agent, AgentExecutor)
│
├── langsmith             可观测性平台 (trace, monitor, evaluate)
│
└── langserve             部署 (FastAPI 集成)
```

### 关键概念区分

| 概念 | 是什么 | 我们是否使用 |
|------|--------|-------------|
| LangChain | 生态品牌名 | ✅ 使用其 langgraph/langchain-core |
| LangGraph | 状态图引擎 | ✅ 核心框架 |
| StateGraph | LangGraph 中的图类型 | ✅ 所有 Agent 的基础 |
| Plan Graph | 我们定义的顶层决策图 | ✅ 主 Agent |
| Execute Graph | 我们定义的执行子图 | ✅ 子 Agent 各自拥有 |
| Agent Loop | 用 Python while 模拟的状态机 | ❌ 已删除（被 LangGraph 替代）|
| ReAct Agent | LangChain 的思考-行动循环模式 | ❌ 不用（用 Plan-then-Execute）|
| MemGPT | 论文提出的虚拟内存管理概念 | ✅ 我们的记忆系统借鉴其设计 |

### 不用 LangChain 的哪些部分

- **不用 `create_react_agent` / `AgentExecutor`**：ReAct 模式（思考→行动→观察→重复）适合开放式探索，但不适合我们结构化的 Plan-then-Execute 流程
- **不用 `langchain-community` 的 LLM 包装**：已有自研 `LLMClientV2`（多供应商、滑动窗口速率限制）
- **不用 `langchain-community` 的 VectorStore 包装**：直接用 `chromadb` 库

---

## 2. Agent 架构：多 Agent 系统

### 2.1 主 Agent

- **唯一常驻进程**。用户唯一对话入口。
- 运行 **Plan Graph**（LangGraph StateGraph）：`parse_intent → clarify → generate_plan → await_approval → execute_plan`
- 执行阶段：创建子 Agent → 监控进度 → 汇总结果 → 报告用户
- 拥有**完整 4 层 MemGPT 记忆**（跨会话持久化）
- 通过 LangGraph `MemorySaver`（SQLite）实现 checkpoint → 进程重启可恢复

### 2.2 IngestAgent（入库子 Agent）

- **固定 7 阶段线性流水线**：search → evaluate → download → convert → index → rank → survey
- Execute Graph 是**线性图**（无条件分支，每阶段完成自动 checkpoint）
- 搜索策略（分几次搜索、关键词选择、已有论文去重）由主 Agent 的 Plan Graph 决定
- 战术执行（下载失败重试、转换质量检查）由 IngestAgent 自己处理
- 轻量操作（search/evaluate/rank）直接执行；重量操作（download/convert/index/survey）分发到 Celery Worker
- 通过 TaskLogger 写 JSON 日志（每 task 独立 `.jsonl` 文件）
- **可同时存在多个实例**（不同项目）

### 2.3 RADQueryAgent（查询子 Agent）

- **动态迭代流程**：parse_intent → route → search → evaluate_completeness → (不够→refine→loop | 够了→format)
- Execute Graph 是**动态图**（条件分支 + 迭代循环）
- 负责：判断查哪个 ChromaDB Collection/项目 → 生成结构化查询 → 检索 → 判断结果充分性 → 格式化
- 不返回原始向量给 LLM（向量只是内部相似度匹配手段）。LLM 收到的是检索出的文本块
- **可同时存在多个实例**（不同查询）

### 2.4 IngestAgent 先查后入的流程

```
主 Agent Plan:
  Step 1: create_query_agent("库里已有的 XX 论文") → 返回已有论文列表
  Step 2: generate_search_strategy(已有列表) → 制定搜索策略
  Step 3: create_ingest_agent(search_strategy, existing_papers=已有列表)
```

### 2.5 Agent 间通信

**所有跨 Agent 通信都经过主 Agent。** 子 Agent 之间不直接对话。

| 模式 | 场景 | 实现 |
|------|------|------|
| 通过主 Agent 协调 | IngestAgent 需要查询结果 | 主 Agent 先创建 QueryAgent → 拿结果 → 传给 IngestAgent |
| 通过共享数据层 | 需要知道彼此的存在 | AgentRegistry / SQLite / JSON 日志 |
| 直接调用 | 同进程内紧耦合 | 不推荐（破坏隔离性） |

### 2.6 Agent 管理：AgentRegistry

不将子 Agent 注册为 Tool。子 Agent 和 Tool 本质不同：

| | Tool | 子 Agent |
|---|------|----------|
| 生命周期 | 单次调用，立即返回 | 长时间运行，有状态 |
| 决策能力 | 无（纯函数执行） | 有自己的 Execute Graph + LLM 判断 |
| 进度报告 | 无 | 结构化 JSON 日志 |
| 暂停/恢复 | 不支持 | 支持 |

子 Agent 通过 **meta-tool** 暴露给 LLM：

```json
// LLM 看到的工具列表包含：
{"name": "create_ingest_agent", "description": "创建入库子Agent...", "parameters": {...}}
{"name": "create_query_agent", "description": "创建查询子Agent...", "parameters": {...}}
```

LLM 以 tool_use 方式调用这些 meta-tool。Handler 内部创建 Agent 实例、注册到 AgentRegistry、启动 asyncio 任务。**从 LLM 视角，这和调用普通 tool 完全一样。** Anthropic Messages API 不需要特殊支持——`tool_use` 的 handler 可以做任何事，包括启动新 Agent。

---

## 3. IngestAgent 是否需要 LangGraph

**IngestAgent 使用 LangGraph 线性图**。7 个节点顺序连接，无条件分支。LangGraph 提供：

1. **自动 checkpoint**：每阶段完成自动保存状态。进程崩溃后从 checkpoint 恢复，不用重跑已完成阶段
2. **中断点 (interrupt)**：可在阶段之间暂停，用户审查后继续
3. **统一框架**：与 RADQueryAgent 共享相同的 LangGraph 基础设施

真正的区别不在"是否用 LangGraph"，而在图的复杂度：
- IngestAgent → 线性图（无分支）
- RADQueryAgent → 动态图（条件边 + 循环）

---

## 4. 意图路由：双层匹配

```
用户消息
  │
  ├── Layer 1: 正则匹配 (硬编码规则)
  │   ├── "搜(索|论文|文献)|调研|survey|literature|综述" → 入库意图
  │   ├── "查(询|看|阅)|什么是|如何|解释|介绍|总结" → 查询意图
  │   └── 不匹配 → 进入 Layer 2
  │
  └── Layer 2: LLM 判断
        System Prompt 收到 Layer 1 的 hint，LLM 自主决定 Agent 类型
```

正则匹配的结果作为 hint 注入 System Prompt，消除 LLM 在明确场景下的路由不确定性。

---

## 5. Clarify 机制：何时询问用户

```
parse_intent → LLM 分析 ambiguity_score:
  ├── score < 0.3 → skip clarify，直接生成 plan
  ├── score > 0.7 → 必须 clarify（意图高度模糊）
  └── 0.3-0.7 → LLM 自主决定
       System Prompt: "如果你有 80% 把握，直接生成 plan。不确定才问。不要为确认而确认。"
```

**强制提问场景**（硬编码规则，不经 LLM 判断）：
- 搜索关键词为空或过于宽泛（如 "AI"、"深度学习"）
- 未指定时间范围且领域快速变化
- 不可逆操作（删除/清理）
- 费用超过阈值（预计下载 >50 篇 PDF）

---

## 6. MemGPT 4 层记忆

| 层级 | 存储 | 生命周期 | 内容 | LLM 管理工具 |
|------|------|----------|------|-------------|
| ShortTerm | 进程内存 (deque) | 当前对话窗口 ~8000 tokens | 完整对话消息 | summarize_memory, delete_memory, tag_memory |
| MidTerm | SQLite (task_checkpoints) | 当前任务 | 任务进度、中间结果、LangGraph checkpoint | 自动（LangGraph MemorySaver） |
| LongTerm | SQLite + ChromaDB | 永久 | 论文知识、对话摘要、用户画像 | extract_to_long_term, search_library, search_knowledge |
| MetaMemory | SQLite (strategy_log + preferences) | 永久 | 策略有效性、错误模式、用户偏好 | get_best_strategy, get_common_errors, get_user_preference |

**记忆分配原则**：主 Agent 拥有完整的 4 层记忆。子 Agent 只拥有任务级记忆（ShortTerm + MidTerm），任务完成后记忆释放。

**MemGPT** 是一篇论文提出的虚拟内存管理概念（"Memory-GPT"），不是开源框架。核心思想是让 LLM 自主管理记忆（什么保留、什么压缩、什么删除），通过工具调用来操作记忆层级。我们借鉴了其分层思想和 LLM 自主管理原则，但 4 层记忆的具体实现是自研的。

---

## 7. 多层容错机制

| 层级 | 场景 | 处理方式 |
|------|------|----------|
| L1 日志审计 | 一切错误 | 写入结构化 JSON 日志，事后可追溯 |
| L2 降级策略 | LLM 不可用 | 规则兜底（评分默认 0.5）、部分功能离线可用 |
| L3 重试机制 | 网络超时、API 限流 | 指数退避 (1s→2s→4s→8s)，最多 3 次 |
| L4 Checkpoint | 进程崩溃 | LangGraph MemorySaver (SQLite) 恢复到崩溃前节点 |
| L5 Agent 级 | 子 Agent 全部失败 | 主 Agent 汇总失败原因 → 告知用户 |
| L6 用户介入 | 策略层面卡住 | 主 Agent interrupt → 询问用户决策 |

---

## 8. Checkpoint：SQLite vs Redis 分工

| | SQLite | Redis |
|---|--------|-------|
| 存什么 | Plan Graph 每个节点的完整 State 快照 | 事件消息（用户消息、Celery 进度、Agent 通知） |
| 粒度 | 每个 node 执行后自动保存 | 每个事件一条 |
| 用途 | **崩溃恢复**："从哪继续" | **跨进程通信**："发生了什么新事情" |
| 持久化 | 磁盘持久 | 可选持久（RDB/AOF） |
| 实现 | LangGraph SqliteSaver | Redis BRPOP/LPUSH |

---

## 9. FastAPI 与 WebSocket 的定位

```
iOS ─┬── WebSocket (直连) ──→ 主 Agent (全双工实时对话)
     │
     └── REST (FastAPI) ────→ 数据查询 (project/paper/task CRUD, 进度日志读取)
```

WebSocket 和 FastAPI 是**同级入口**，不经由彼此：
- WebSocket: 实时对话通道（LLM streaming、进度推送、用户消息）
- REST: 无状态数据查询（项目列表、论文详情、进度日志拉取、导出）

---

## 10. 进度汇报：结构化 JSON 日志

### 事件类型

| event | 含义 |
|-------|------|
| `task_start` | 子 Agent 启动 |
| `stage_start` | 进入新阶段 |
| `stage_progress` | 阶段级进度 |
| `paper_progress` | 单篇论文操作 |
| `stage_done` | 阶段完成 |
| `task_done` | 全流程完成 |
| `task_error` | 任务级错误 |

### 文件组织

```
~/.paper_search/logs/
├── agent.log                # Agent 全局日志 (JSONL)
└── tasks/
    └── task-{YYYYMMDD}-{序号}.jsonl  # 每个子 Agent 独立日志
```

---

## 11. 需要删除的代码（已完成）

| 文件 | 原因 |
|------|------|
| `agent.py` (ResearchAgent) | 旧 8 阶段 Agent，已被 MCP 工具替代 |
| `agent_loop.py` (AgentLoop) | 自研状态机，被 LangGraph Plan Graph 替代 |
| `auto_pipeline.py` (AutoPipeline) | 7 阶段硬编码流水线，被 IngestAgent Execute Graph 替代 |
| `tool_registry.py` (ToolRegistry) | 空壳单例，无工具注册。被 LangChain BaseTool + 分治 Registry 替代 |

---

## 12. 技术栈总览（最终版）

| 层级 | 技术 | 状态 |
|------|------|------|
| Agent 框架 | LangGraph (StateGraph) | 新增 |
| 工具系统 | LangChain BaseTool | 新增 |
| LLM 客户端 | LLMClientV2 (自研，兼容 Anthropic API) | 保持 |
| 消息队列 | Redis (BRPOP/LPUSH) | 新增 |
| 异步任务 | Celery + Redis Broker | 新增 |
| 定时任务 | Celery Beat | 新增 |
| Web 框架 | FastAPI + WebSocket | 新增 |
| 向量数据库 | ChromaDB (双 Collection) | 保持 |
| 关系数据库 | SQLite (check_same_thread=False) | 保持 |
| 搜索引擎 | 6 个 Provider | 保持 |
| PDF 转换 | pymupdf4llm | 保持 |
| 进度日志 | TaskLogger + JSONL | 新增 |
| 子 Agent 编排 | Python asyncio + LangGraph Execute Graph | 新增 |
| 记忆系统 | 自研 4 层 (借鉴 MemGPT 概念) | 保持 |
| 部署 | Docker + docker-compose | 新增 |
| iOS 推送 | APNs | 新增 |

---

## 13. 架构全景图

```
                        ┌──────────────────────┐
                        │       用户 (iOS)      │
                        └──────────┬───────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    │ WebSocket    │    REST       │
                    │ (实时对话)   │   (数据查询)   │
                    └──────┬───────┴──────┬───────┘
                           │              │
              ┌────────────┴──────────────┴───────────┐
              │          主 Agent (常驻，有 checkpoint)  │
              │                                        │
              │  Plan Graph (LangGraph)                │
              │  parse_intent → clarify → plan → exec  │
              │                                        │
              │  完整记忆: ShortTerm + MidTerm         │
              │           + LongTerm + MetaMemory      │
              │                                        │
              │  Meta-Tools:                          │
              │    create_ingest_agent                │
              │    create_query_agent                 │
              │    agent_status / agent_cancel         │
              └────┬──────────────┬──────────────────┘
                   │              │
       ┌───────────┴──┐    ┌──────┴───────────┐
       │ IngestAgent  │    │  RADQueryAgent   │
       │ (可多个)      │    │  (可多个)         │
       │               │    │                  │
       │ Execute Graph │    │ Execute Graph    │
       │ (线性 7 阶段) │    │ (动态迭代)        │
       │               │    │                  │
       │ 任务级记忆     │    │ 任务级记忆        │
       └──────┬────────┘    └──────┬───────────┘
              │                    │
              └────────┬───────────┘
                       │
            ┌──────────┴──────────┐
            │     共享层           │
            │  AgentDB | ChromaDB │
            │  Engine  | TaskLogger│
            │  Redis   | Celery   │
            └─────────────────────┘
```

---

> 版本: v1.0 | 来自 2 天架构讨论的知识沉淀 | 2026-06-15
