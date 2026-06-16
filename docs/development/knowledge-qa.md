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

## 14. langchain-community 统一 LLM 接口详解

### 设计模式

`langchain-community` 通过 `BaseChatModel` 基类统一所有 LLM 调用：

```python
# OpenAI 协议（包括火山引擎、任何 /v1/chat/completions 兼容端点）
from langchain_community.chat_models import ChatOpenAI
llm = ChatOpenAI(model="deepseek-v4-pro", base_url="https://ark.cn-beijing.volces.com/v2")

# Anthropic 协议（/v1/messages 端点，tool_use 格式不同）
from langchain_community.chat_models import ChatAnthropic
llm = ChatAnthropic(model="claude-sonnet-4-6")

# 统一调用
llm.invoke([HumanMessage("Hello")])
await llm.ainvoke([...])    # 异步
llm.stream([...])           # 流式 (SSE)
```

### 火山引擎兼容性

火山引擎 `codingplan` 端点支持 **OpenAI-compatible 协议**（`/v1/chat/completions`），可以使用 `ChatOpenAI`。部分端点也支持 **Anthropic-compatible 协议**（`/v1/messages`，tool_use 格式），可使用 `ChatAnthropic`。

### 我们是否需要切换？

**当前不需要。** `LLMClientV2` 已经直接用 `httpx` 处理了这两种协议，包括流式、重试、速率限制。切换的时机是 LangGraph 接入时——LangGraph 的 streaming 和 checkpoint 与 `BaseChatModel` 集成最深。届时可以让 `LLMClientV2` 实现 `BaseChatModel` 接口，不改底层 HTTP 逻辑。

### 向量和 LLM 的关系

LLM 接口不处理向量。Embedding 是**独立模型调用**（如 `text-embedding-3-small`），ChromaDB 自带 embedding function 处理，不经过 LangChain。流程是：查询文本 → ChromaDB embedding function → 向量检索 → 返回文本块 → 文本块喂给 LLM。LLM 从头到尾不接触数学向量。

---

## 15. ReAct vs Tool-calling vs Plan-then-Execute

### ReAct (Reasoning + Acting)

```
Thought → Action → Observation → Thought → Action → ... (LLM 自行决定何时停止)
```

- 开放式探索，适合复杂多步推理
- 每步都有 Thought（推理过程），token 消耗大
- 控制性低：LLM 可能走偏方向或陷入循环
- LangChain 提供 `create_react_agent`

### Tool-calling Agent

```
用户请求 → LLM 决定调用哪些工具 → 执行 → 返回结果 (通常 1-2 轮)
```

- 直接高效，适合明确任务
- 无显式推理过程
- 控制性高：工具集受限，调用模式简单
- LangChain 提供 `create_tool_calling_agent`

### Plan-then-Execute（我们的模式）

```
解析意图 → 生成完整计划 → 用户确认 → 逐步执行 → 验证 → 汇总
```

- 结构化分批执行，计划先于行动
- 每步有验证和重试，失败有明确处理路径
- 人机交互点明确（plan 确认、失败求助）
- 基于 LangGraph StateGraph，不基于 ReAct 或 Tool-calling Agent

### 对比总结

| | ReAct | Tool-calling | Plan-then-Execute |
|---|-------|-------------|-------------------|
| 决策模式 | 每步即时决策 | 一次性决策 | 先规划后执行 |
| 迭代次数 | 不固定（LLM决定） | 1-2 轮 | 固定（按plan执行） |
| Token 消耗 | 高（每轮Thought） | 低 | 中（Plan 生成 + 验证） |
| 可控性 | 低 | 高 | 最高 |
| 适用场景 | 开放式探索 | 明确任务 | 结构化复杂任务 |
| 人机交互 | 无明确点 | 无 | 明确暂停点 |

### LangChain 生态覆盖情况

| 组件 | 我们有吗 | 需要吗 |
|------|---------|--------|
| langchain-core (BaseTool, BaseMessage, Runnable) | 无 | ✅ LangGraph 依赖 |
| langgraph (StateGraph, interrupt, MemorySaver) | 设计中 | ✅ 核心框架 |
| langsmith (trace, monitor, evaluate) | 无 | ✅ 调试和观测必需 |
| langserve (FastAPI 集成) | 无 | ❌ 已有自研 FastAPI |
| langchain (ReAct, Chain) | 无 | ❌ 用 Plan-Execute 替代 |
| langchain-community (LLM 包装) | 无 | ⚠️ LLMClientV2 可替代 |
| langchain-community (VectorStore 包装) | 无 | ❌ 直接用 chromadb 库 |

---

## 16. LangSmith — 调试与观测层

### 定位

LangSmith 是 LangChain 生态的**可观测性平台**，不是容错机制本身。容错在系统内处理（重试/降级/checkpoint），LangSmith 帮助开发者**诊断**为什么出错。

### 核心能力

| 能力 | 用途 |
|------|------|
| Trace 视图 | 每次 LLM 调用的完整链：输入 prompt → 输出 → tool 调用 → 耗时 → 费用 |
| Dataset | 创建测试用例集，每次改 prompt 后自动跑全量对比 |
| Monitor | 线上实时监控，异常检测（错误率突增、延迟飙升） |
| Hub | 社区 prompt 模板，可发布和复用 |

### 与 JSON 日志的分工

| | LangSmith | JSON 日志 |
|---|-----------|-----------|
| 对象 | 开发者（调试） | 系统（业务审计）+ iOS（进度展示） |
| 内容 | LLM 调用链、token 数、延迟 | 业务事件：task_start, paper_progress, task_done |
| 持久化 | LangSmith 云端 | 本地磁盘 (`~/.paper_search/logs/`) |
| 启用 | `LANGCHAIN_TRACING_V2=true` | 始终启用 |

---

## 17. 主 Agent 调 Tool ≠ Execute Graph

### 区分原则

| 操作 | 属于什么 | 特征 |
|------|---------|------|
| `llm.chat()` | Plan Graph 节点内的单次 LLM 调用 | 无状态追踪、无重试、无进度 |
| `paper_status(project_id)` | Plan Graph 节点内的直接 tool 调用 | 瞬时完成、无中间状态 |
| `get_paper_abstract(paper_id)` | Plan Graph 节点内的直接 tool 调用 | 瞬时完成、无中间状态 |
| `create_ingest_agent(...)` | Plan Graph 创建子 Agent | 子 Agent 自带 Execute Graph |
| `create_query_agent(...)` | Plan Graph 创建子 Agent | 子 Agent 自带 Execute Graph |

### 判断标准

是否需要以下任一能力：
- 多步状态追踪
- 进度汇报
- 暂停/恢复
- 验证+重试

满足任一 → 子 Agent（有 Execute Graph）。都不满足 → 主 Agent 直接调 tool。

### 简单 CLI / Read / Bash 谁执行

| 操作类型 | 执行者 |
|----------|--------|
| 瞬时查询（<1秒） | 主 Agent Plan Graph 节点内直接调 |
| 简单 1-3 步操作 | 主 Agent Plan Graph 节点内直接调 |
| 复杂多步/长时操作 | 创建子 Agent（自带 Execute Graph） |

---

## 18. 意图判断：正则 + LLM 双层（不需要第三模型）

### 为什么不需要小 NLP 模型

Plan Graph 第一个节点 `parse_intent` 本身就是 LLM 调用，**LLM 是最强的语义理解模型**。不需要额外引入 BERT 或小型分类器。

### 双层设计

```
用户消息
  │
  ├── Layer 1: 正则 (关键词匹配) — <1ms
  │   匹配明确模式 → 跳过 LLM 调用，直接路由
  │   不匹配 → 进入 Layer 2
  │
  └── Layer 2: LLM parse_intent — ~500ms
      输出: {intent_type, ambiguity_score, ...}
      用于澄清决策和 Agent 路由
```

### 各手段对比

| 手段 | 延迟 | 准确率 | 部署成本 |
|------|------|--------|----------|
| 正则 | <1ms | ~95%（明确模式）| 零 |
| LLM parse_intent | ~500ms | ~95%+（含语义模糊判断）| 零（已有 LLM） |
| 小 NLP 模型 (BERT) | ~50ms | ~90%（需训练数据）| 需模型部署和训练数据 |

**结论：正则 + LLM 双层足够。** 正则快速筛选，LLM 精准判断。不引入第三个模型。

---

## 19. OpenAIEmbeddings — Embedding 模型技术选型

### 是什么

`OpenAIEmbeddings` 是一个**文本 → 数学向量**的服务。不是 LLM（不做推理、不做对话），只做一件事：把文本映射到高维向量空间中的一个坐标点。

```
文本 → [0.023, -0.451, 0.891, ..., 0.337] ← 1024 个浮点数
```

### 嵌入的对象

嵌入的是**文本的语义含义**。不是单词、不是文档 ID，是文本表达的意思。意思相近的文本，向量距离近；意思无关的文本，向量距离远。

### 与 RAD（ChromaDB）的关系

```
入库: 论文文本 → OpenAIEmbeddings → 向量 → ChromaDB 存储
查询: 用户问题 → OpenAIEmbeddings → 查询向量 → ChromaDB 相似度搜索 → 返回文本块
```

OpenAIEmbeddings 是翻译器（文本→向量），ChromaDB 是存储引擎（存向量+搜向量）。

### 向量不只是搜索用

| 用途 | 说明 |
|------|------|
| 语义搜索（主要） | 中文查英文、模糊概念匹配 |
| 论文去重 | 向量相似度 > 0.95 → 可能是同一篇 |
| 主题聚类 | K-means 聚类 → 自动发现研究方向 |
| 异常检测 | 某篇向量和所有其他论文距离都很大 |
| 可视化 | t-SNE/UMAP 降维生成论文分布图 |

### 向量空间一致性

**入库和查询必须用同一个 Embedding 模型。** 不同模型的向量空间完全不同，无法比较。

```
❌ 入库用 MiniLM (384维)，查询用 text-embedding-3 (1024维) → 维度不同，无意义
✅ 入库和查询都用 text-embedding-3-small (1024维) → 同一空间，相似度有效
```

不存在跨模型的向量翻译方法。切换 embedding 模型 → 必须重新嵌入所有文档。

### 不同模型的相似度差异

同一对论文，不同 embedding 模型给出完全不同的相似度：

| 论文对 | MiniLM-L6-v2 | text-embedding-3-small | bge-large-zh |
|--------|-------------|----------------------|-------------|
| 中文查英文(Transformer) | 0.52 | 0.78 | 0.81 |
| 同领域不同方法 | 0.71 | 0.83 | 0.79 |
| 完全不同领域 | 0.12 | 0.08 | 0.15 |

**中英混合场景下，text-embedding-3-small 显著优于本地小模型。这是选型的核心原因。**

### 选型决策

| 维度 | 本地 MiniLM | OpenAI text-embedding-3-small |
|------|-----------|------------------------------|
| 维度 | 384 (固定) | 512-1536 (可配置) |
| 中英混合 | 一般 | 优秀 |
| 部署 | 本地，零延迟 | API，~50ms |
| 费用 | 免费 | ~$0.02/1M tokens |
| LangSmith trace | ❌ | ✅ 自动 |

**结论：切换到 text-embedding-3-small**。已入库数据通过 `index_paper --all` 一键重建。

---

## 20. Plan-then-Execute 的结构强制 vs Prompt 软约束

### Claude Code 中火山引擎来来回回的原因

火山引擎 (deepseek-v4-pro) 的 tool-use 训练倾向于"走一步看一步"。这不是 ReAct 模式的问题，是模型缺乏 Plan-first 倾向。在 Claude Code 中，模型自由决定何时调用工具、调用多少次。

### 软约束方案（在 Claude Code 中）

- System Prompt 强制：在 CLAUDE.md 中写明"先规划全部工具调用，再一次性执行"
- 合并批量工具：把多步操作封装成一个工具，减少调用次数

**局限：模型可能仍然不遵守。**

### 硬约束方案（LangGraph Plan Graph）

```python
# Plan Graph 的结构强制执行 Plan-First
plan_graph.add_node("generate_plan", generate_plan_node)  # 只能输出 plan JSON，不能调工具
plan_graph.add_node("await_approval", interrupt_node)      # 必须等用户确认

# 工具调用被限制在 execute_plan 阶段
# LLM 无法在 generate_plan 中调用工具
# 这是结构级别的强制，而非 prompt 级别的请求
```

| | Claude Code (软约束) | LangGraph (硬约束) |
|---|---------------------|-------------------|
| Plan 生成 | Prompt 请求 | 图结构强制 |
| 用户确认 | 取决于模型 | interrupt 节点强制暂停 |
| 工具调用 | 模型自主 | 仅在 execute_plan 允许 |
| 违规可能 | 模型可能忽略 | 图结构不允许 |

---

## 21. 三层验收体系

### 架构

```
L2: Plan Graph overall_evaluate (战略验收)
    ├── satisfied → END
    └── not → loop to generate_plan (调整策略)

L1: Execute Graph step_verify (战术验证)
    ├── pass → next_step
    ├── retry (≤2) → loop to tool_execute
    └── fail → alert main agent

L3: Sub-agent 内置自检 (操作级)
    纯规则，无 LLM。检查 PDF 有效性、Markdown 质量等
```

### L1: Execute Graph 步骤验证

```python
execute_graph.add_conditional_edges("step_verify", decide_after_verify, {
    "pass": "collect_metrics",
    "retry": "tool_execute",      # 最多 2 次
    "fail": "alert_main_agent",
})

def decide_after_verify(state):
    verification = llm.verify_step(
        expected=state.expected_output,
        actual=state.last_result,
        retry_count=state.retry_count,
    )
    if verification.passed: return "pass"
    if verification.retryable and state.retry_count < 2: return "retry"
    return "fail"
```

### L2: Plan Graph 整体验收

```python
plan_graph.add_conditional_edges("overall_evaluate", decide_overall, {
    "satisfied": END,
    "adjust": "generate_plan",   # ← 循环回 plan 生成
})

def decide_overall(state):
    evaluation = llm.evaluate_overall(
        original_query=state.original_query,
        plan=state.plan,
        results=state.step_results,
    )
    # 检查: 数量够吗？质量达标吗？覆盖面全吗？下载/索引完整吗？
```

### L3: 子 Agent 内置自检

纯规则，不调 LLM（避免延迟和 token 消耗）：

```python
async def _self_check_download(self, results):
    success_rate = sum(1 for r in results if r.success) / len(results)
    if success_rate >= 0.8: return CheckResult.pass
    if success_rate >= 0.5: return CheckResult.warn
    return CheckResult.fail

async def _self_check_markdown(self, md_path):
    size = md_path.stat().st_size
    if size < 500: return CheckResult.fail  # 太小，转换失败
    return CheckResult.pass
```

### 验收失败处理路径

```
L3 自检 fail → 标记 paper_progress failed，跳过，继续下一批
L1 验证 fail → retry (≤2) → 仍 fail → 主 Agent 介入
L2 验收 fail → 主 Agent 分析原因 → 重新生成 plan 或询问用户
```

---

---

## 22. MemGPT 与对话摘要记忆机制

### 来源

MemGPT（"Memory-GPT"）是 UC Berkeley 2023 年的论文《MemGPT: Towards LLMs as Operating Systems》。开源项目 `github.com/cpacker/MemGPT`，现已更名为 **Letta**。我们借鉴其虚拟内存管理理念，但 4 层记忆为自研实现。

### 核心类比

```
操作系统虚拟内存 → LLM 记忆管理
─────────────────────────────────
物理内存 (RAM)    → 上下文窗口 (~8000 tokens)
虚拟内存 (磁盘)    → SQLite + ChromaDB (无限容量)
页面置换算法      → LLM 自主调用 summarize/delete/extract
缺页中断          → LLM 调用 search_memory 检索旧信息
```

### 对话摘要就是记忆压缩

```
长对话 (500 轮):
  ShortTerm 窗口: 最近 10 轮完整保留
                  + 旧对话被 LLM 压缩为 Markdown 摘要
  MidTerm:        当前任务 checkpoint
  LongTerm:       提取的重要发现 → ChromaDB 语义可检索
  MetaMemory:     策略有效性 + 用户偏好

当 token 超限:
  LLM 自主调用:
    summarize_memory(老消息) → 更新 Markdown 摘要
    extract_to_long_term(重要发现) → 持久化到 ChromaDB
    delete_memory(冗余消息) → 彻底移除
```

**对话摘要 MD 就是一种记忆机制。** 不是简单截断旧消息，而是让 LLM 主动压缩、分类、持久化，形成可检索的知识。

---

## 23. 搜索来源策略（历史方案 → 已修订为 Semantic Scholar P0）

> ⚠️ 本节记录原始方案。最终决策见 §27：Google Scholar 已放弃，改用 Semantic Scholar P0 + 多源降级。

### 原始设计（已废弃的 Google Scholar P0）

原计划 Google Scholar 优先获取元数据 → 其他来源并行补充。因 Google Scholar 无官方 API、反爬维护成本高、搜索结果不含完整摘要，已改为 Semantic Scholar 作为 P0 主来源。保留本节作为技术决策的历史记录。

### 当前生效方案（§27）

```
P0: Semantic Scholar → 元数据主来源 (2亿+，完整摘要 + AI排序 + 引用 + OA PDF)
P1: arXiv + PubMed → 并行补充
P2: OpenAlex + ScienceDirect → 广度补充
P3: IEEE Xplore + CNKI → 按需

PDF 下载顺序: S2 OA → arXiv direct → ScienceDirect → IEEE → publisher page
```

### PDF 不可用追踪

```
SQLite 表: unavailable_pdfs
  project_id, paper_id, title, tried_sources (JSON), reason, created_at

API:
  GET /api/projects/{id}/unavailable_pdfs
    → {papers: [{title, tried_sources, reason, suggested_action}]}

Agent 能力:
  paper_status(project_id) 返回中包含 unavailable_count
  Agent 可建议: "有 3 篇论文 PDF 不可用。是否尝试通过机构访问或联系作者？"
```

---

## 24. CitationChaseAgent（以文搜文 Agent）

### 定位

独立的子 Agent，负责「从一篇种子论文出发，沿引用关系追溯相关文献」。有自己的 **Execute Graph**（动态图）。

### 来源策略

```
fetch_relations 节点:
  ├── P0: Semantic Scholar API
  │     → citations + references + related papers
  │
  └── P1: arXiv + PubMed 并行补充
        └── 合并去重
```

### Execute Graph（完整流程）

```
START
  │
  ▼
resolve_seed               # DOI/标题 → 完整元数据
  │
  ▼
check_library               # 查询库内是否已有
  │
  ▼
fetch_relations ──────────────────────────────────┐
  │  P0: Semantic Scholar API                      │
  │  P1: arXiv + PubMed (并行)                     │
  │  → 合并去重 (DOI + 标题 + 向量)                 │
  ▼                                                │
┌──────────────────────────────────────────────┐   │
│ parallel (3路并发 LLM 评估):                  │   │
│  ├── evaluate_citing     (前向引用)           │   │
│  ├── evaluate_referenced (后向引用)           │   │
│  └── evaluate_related    (语义相关)           │   │
│                                               │   │
│  评估维度: 主题相关性 + 期刊等级 + 引用量      │   │
│           + 是否库内已有 + 经典程度            │   │
└──────────────────────────────────────────────┘   │
  │                                                │
  ▼                                                │
filter_and_rank            # 合并排序 → 去重       │
  │                                                │
  ▼                                                │
┌──────────────────────────────────────────────┐   │
│ parallel (N篇入选论文):                       │   │
│   download (优先 S2 OA → arXiv → publisher)    │   │
│   → convert (pymupdf4llm)                     │   │
│   → index (ChromaDB)                          │   │
│   全部失败 → 记录 unavailable_pdfs            │   │
└──────────────────────────────────────────────┘   │
  │                                                │
  ▼                                                │
decide_next_layer          # LLM 判断是否继续
  ├── current_depth < max_depth? (默认2, 用户可指定)
  ├── 本层高相关论文 ≥ 阈值?
  ├── 相关性衰减 < 阈值? (层2的相关性比层1显著降低 → 停止)
  │
  ├── continue → 高相关论文作为新种子 → loop
  └── stop → summarize → END
  │
  ▼
summarize                  # 产出: Markdown 报告 + 引用网络 JSON
END
```

### 并行机会

```
同一层内:
  fetch_relations: 多来源并行 (Semantic Scholar + arXiv + PubMed)
  evaluate:        3路 LLM 评估并行 (citing / referenced / related)
  ingest:          N篇论文下载+转换+索引 并行 (受 Semaphore 控制)

跨层:
  追溯层之间必须串行 (层2 依赖 层1 的结果)
```

### 去重处理

```
tracked_ids: set[paper_id]   # 全局已处理，跨层去重
重叠引用: 论文 D 同时被 A 和 B 引用 → 只处理一次
库内已有: 跳过下载和转换，直接关联
```

### 异常处理

| 异常 | 处理 |
|------|------|
| 种子 DOI 无效 | task_error，立即终止 |
| Semantic Scholar API 超时/限流 | 指数退避重试，降级到 arXiv + PubMed + OpenAlex |
| API 全部超时/限流 | 指数退避重试，最多 3 次 |
| 某篇论文无 PDF | 标记 unavailable，继续 | 
| 循环引用 | tracked_ids 去重 |
| 追溯结果 0 篇 | 报告用户，建议扩大来源 |
| 下载全部失败 | 暂停当前层，LLM 判断→调整策略或终止 |

---

## 25. HistoryAgent（历史消息处理 Agent）

### 定位

专门处理历史消息的子 Agent。有自己的 **Plan Graph + Execute Graph**。

### 职责

```
主 Agent 重启后:
  1. 从 Redis 恢复所有未处理消息
  2. 优先处理最新 Plan-Execute 相关消息
  3. 其余消息 → 创建 HistoryAgent ──────────────────┐
                                                      │
  HistoryAgent Plan Graph:                            │
    分析消息列表 → 生成处理计划                        │
      - 哪些消息重复? → 合并                           │
      - 哪些消息已过期? → 归档为摘要                   │
      - 哪些消息已被处理过? → 标记不重复处理            │
      - 哪些消息仍需处理? → 按优先级排序               │
                                                      │
  HistoryAgent Execute Graph:                         │
    for each 消息组:                                   │
      归档过期的 → summarize → LongTerm               │
      合并重复的 → 保留最新的                          │
      标记已处理的 → 跳过                              │
      处理待办的 → 生成 action list → 通知主 Agent     │
                                                      │
  完成后 → 通知主 Agent → 主 Agent 汇总结果            │
```

### 为什么需要自己的 Plan Graph

不同于 IngestAgent（固定 7 阶段）和 RADQueryAgent（固定检索流程），HistoryAgent 面对的是**无结构的历史消息集合**。它需要先分析（Plan）——哪些是重复的、哪些过期、哪些需要处理——然后执行。Plan Graph 让 LLM 在动手前先理解全貌。

---

---

## 26. TranslationAgent + 术语库设计

### 定位

工具型 Agent（无 Execute Graph），负责中英文学术术语的翻译和术语库维护。不参与 Agent 调度体系——它是一个被其他 Agent 调用的服务。

### 为什么需要

火山 Embedding 模型虽有多语言能力，但学术术语的精确翻译需要领域知识。通过 LLM 翻译 + 术语库校准，确保"对比学习"被正确翻译为"contrastive learning"而非字面翻译。

### 术语库设计

```
SQLite 表: terminology
  id, term_cn, term_en, context (学术语境),
  source_papers (JSON: [paper_id]), frequency,
  confidence (0-1), created_at, updated_at

ChromaDB Collection: agent_terminology
  内容: term_cn + " → " + term_en + " (" + context + ")"
  用途: 用户查询时语义搜索匹配术语，辅助 LLM 翻译
```

### 术语来源

| 来源 | 提取方式 |
|------|----------|
| 论文标题 | 部分论文同时有中英文标题 → 直接提取对应关系 |
| 论文关键词 | author keywords 字段 → 高频术语 |
| 论文摘要 | LLM 提取关键术语 → 按频率排序 → 人工修正 |
| 用户查询 | 用户搜索词 + 最终找到的英文论文标题 → 隐式对应 |

### 工作流程

```
入库后:
  IngestAgent 完成 → 触发 TranslationAgent.extract_terms(project_id)
    → 从新入库论文中提取术语 → 更新术语库

查询时:
  RADQueryAgent 收到中文查询
    → 调用 TranslationAgent.translate_query(cn_query)
      → 1. 查术语库精确匹配
      → 2. ChromaDB agent_terminology 语义搜索
      → 3. LLM 翻译（术语库上下文作为 few-shot）
    → 返回学术英文查询 → 继续 Embedding → 检索

用户可交互:
  GET /api/terminology → 查看术语库
  POST /api/terminology/{id}/correct → 人工修正翻译
```

### 与 Embedding 可插拔设计的关系

```
查询管道:
  中文查询 → TranslationAgent (术语库+LLM) → 学术英文
    → [Embedding 可插拔层: 火山 | DeepSeek | OpenAI]
      → 向量 → ChromaDB 检索
```

---

## 27. Agent Manifest — 身份证与启动协议

### 用途

`agent_manifest.json` 是 Agent 的身份证 + 启动说明书。不是记忆的一部分——记忆在 MemGPT 中。

### 核心字段

- `agent.agent_id`：全局唯一标识，首次启动生成
- `runtime`：启动入口、Plan Graph 路径、checkpoint 位置、LLM 配置
- `memory`：4 层记忆的存储位置（SQLite 表、ChromaDB collections）
- `sessions`：活跃 session 列表
- `migration`：版本兼容性 + 数据校验

### 启动流程

```
manifest 存在 → 验证兼容 → 初始化组件 → 从 checkpoint 恢复 → 加载 MemGPT → 就绪
manifest 不存在 → 创建主 Agent → 初始化空白 DB/ChromaDB → 写 manifest → 就绪
```

详见 `docs/development/agent-manifest.md`。

## 28. Session 设计 — agent_id + session_id 双层

### 连接格式

```
ws://{host}:{port}/ws/chat/{agent_id}/{session_id}
```

一个 Agent 多个 session（隔离上下文）：

| 记忆层 | main session | 命名 session | temp session |
|--------|-------------|-------------|-------------|
| ShortTerm | ✅ 独立 | ✅ 独立 | ✅ 当前，断开丢弃 |
| LongTerm | ✅ 自动写 | ⚠️ 手动 promote | ❌ |
| RAD | ✅ 全局共享 | ✅ 全局共享 | ✅ 全局共享 |
| MetaMemory | ✅ 学习 | ❌ | ❌ |

### API

- `GET /api/agents` — Agent 列表
- `GET /api/agents/{id}/sessions` — Session 列表（含自动生成的标题）
- `POST /api/agents/{id}/sessions` — 创建新 session

### 自动标题

参考腾讯元宝：第一条消息后 LLM 自动生成 ≤10 字标题写入 sessions 表。

## 29. 最终技术决策（2026-06-16 定稿）

### 搜索来源

```
P0: Semantic Scholar → 元数据主来源 (2亿+，完整摘要 + AI排序 + 引用关系 + OA PDF)
P1: arXiv + PubMed → 并行补充
P2: OpenAlex + ScienceDirect → 广度补充
P3: IEEE Xplore + CNKI → 按需补充
[已移除] Google Scholar → 无官方API，反爬维护成本高，放弃
```

### Embedding 供应商

```
主: 火山引擎 doubao-embedding-vision
      model: doubao-embedding-vision
      base_url: https://ark.cn-beijing.volces.com/api/plan/v3
      api_key: 同火山引擎 API Key
      维度: 1024
      特点: 多模态(文本+图片+视频)，中文优化，与LLM同供应商

备: DeepSeek deepseek-embedding-v1
      base_url: https://api.deepseek.com/v1/embeddings
      维度: 1024
      特点: 中文准确率比OpenAI高15-20%，成本$0.0002/1K tokens

实现: 通过 OpenAIEmbeddings 包装（改 base_url），LangChain兼容
      切换供应商 = 改 model + base_url，无需改代码结构
```

### Agent 体系

| Agent | Graph | 触发时机 |
|-------|-------|----------|
| 主 Agent | Plan Graph | 常驻，每次用户消息 |
| IngestAgent | Execute Graph (线性7阶段) | 用户确认入库计划 |
| RADQueryAgent | Execute Graph (动态迭代) | 用户查询知识 |
| ClusteringAgent | Execute Graph (线性5阶段) | 入库完成后自动 |
| CitationChaseAgent | Execute Graph (动态迭代) | 用户以文搜文 |
| HistoryAgent | Plan Graph + Execute Graph | 主Agent重启时 |
| TranslationAgent | 无 Graph (工具型 Agent) | 查询翻译 / 入库后术语提取 |

### LLM 架构

```
BaseChatModel 实现 (VolcanoChatModel):
  实现 langchain_core.language_models.BaseChatModel
  内部保留 httpx 连接池 + 速率限制 + SSE解析
  删除所有高层方法 (chat/chart_json/evaluate_batch等)
  由 LangGraph ToolNode + Plan Graph 节点替代
```

---

> 版本: v1.6 | Agent Manifest、agent_id+session_id、Session内存隔离、启动协议 | 2026-06-16
