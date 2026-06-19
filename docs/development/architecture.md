# Paper Agent v3 — 技术架构与实施方案

> 从 Claude Code Harness → 自研 Agent 系统的完整演化 | 2026-06-14

---

## 1. 系统拓扑

### 1.1 单 Agent 内部结构

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
              ┌─────────────────┴──────────────────────────┐
              │         Agent 守护进程 (daemon.py)          │
              │                                             │
              │  ┌──────────────────────────────────────┐   │
              │  │  AgentRunLoop (事件驱动主循环)        │   │
              │  │  PriorityQueue ← WS/EventBus/Redis/  │   │
              │  │  Timer 四个事件源                     │   │
              │  └──────────────────────────────────────┘   │
              │  ┌──────────────────────────────────────┐   │
              │  │  PlanGraph (主Agent 决策)             │   │
              │  │  Parse → Clarify → Plan → Execute    │   │
              │  └──────────────────────────────────────┘   │
              │  ┌──────────────────────────────────────┐   │
              │  │  7 个子 Agent (IngestAgent 等)       │   │
              │  └──────────────────────────────────────┘   │
              │  ┌──────────────────────────────────────┐   │
              │  │  ToolRegistry (50+ 工具)              │   │
              │  │  纯内存直接调用 / 其余走 Celery       │   │
              │  └──────────────────────────────────────┘   │
              │  ┌──────────────────────────────────────┐   │
              │  │  MemoryManager (4层记忆)              │   │
              │  └──────────────────────────────────────┘   │
              └─────────────┬────────────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              │      Redis (共享)          │
              │  ┌──────────────────────┐  │
              │  │ agent:events:{id}    │  │
              │  │ agent:cmd:{id}       │  │
              │  └──────────────────────┘  │
              └─────────────┬─────────────┘
                            │
         ┌──────────────────┼──────────────────┐
         │                  │                  │
  ┌──────┴──────┐   ┌──────┴──────┐   ┌──────┴──────┐
  │  SQLite     │   │  ChromaDB   │   │  Celery     │
  │  (agent.db) │   │  (6 colls)  │   │  Worker 池  │
  └─────────────┘   └─────────────┘   └─────────────┘
```

### 1.2 多 Agent 部署

```
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│ agent-cv :8001   │  │ agent-nlp :8002  │  │ agent-001 :8000  │
│   独立 daemon     │  │   独立 daemon     │  │   独立 daemon     │
│   独立 agent.db   │  │   独立 agent.db   │  │   独立 agent.db   │
│   独立 PlanGraph  │  │   独立 PlanGraph  │  │   独立 PlanGraph  │
└────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    │    Redis (共享)      │
                    │    Celery Worker 池   │
                    └─────────────────────┘

---

## 2. AgentRunLoop — 事件驱动主循环

> 详见 `docs/development/agent-runloop.md`

```
┌──────────────────────────────────────────────────────────────────┐
│               AgentRunLoop (asyncio PriorityQueue)               │
│                                                                  │
│   4 个事件源 (后台协程，互不阻塞):                                 │
│   ┌─────────────┐ ┌─────────────┐ ┌──────────────┐ ┌──────────┐ │
│   │ _ws_source  │ │_eventbus_src│ │ _redis_source│ │_timer_src│ │
│   │ (prio=0)    │ │ (prio=1~2)  │ │ (prio=1~2)   │ │(prio=3)  │ │
│   └──────┬──────┘ └──────┬──────┘ └──────┬───────┘ └─────┬────┘ │
│          │               │               │               │      │
│          └───────────────┴───────────────┴───────────────┘      │
│                              │                                   │
│                              ▼                                   │
│          while running:                                          │
│            priority, seq, event = await queue.get()  ← 休眠     │
│            plan_graph.dispatch(event)               ← 逐个处理   │
└──────────────────────────────────────────────────────────────────┘

优先级:
  prio=0  user_message / ios_tool_result     ← iOS 用户，立即处理
  prio=1  celery_done / celery_error          ← 子Agent 完成，尽快
  prio=2  celery_progress / tool_start/end    ← 进度状态，可批量
  prio=3  timer_fired                        ← 定时任务触发

Fire-and-Subscribe 模式:
  所有慢操作 (IO/网络/LLM) → celery.send_task() → 立即返回
  PlanGraph 不阻塞等待 → RunLoop 继续处理下一个事件
  任务完成 → EventBus 回传 → PlanGraph 处理结果
```

### 2.1 前台/后台自动判定

| 工具 | 前台阈值 | 规则 |
|------|---------|------|
| `search_papers` | ≤10 篇 | 超过阈值自动后台 |
| `evaluate_papers` | ≤5 篇 | 超过阈值自动后台 |
| `download_paper` | ≤1 篇 | 超过阈值自动后台 |
| 其他 | — | 默认后台执行 |

用户可通过自然语言覆盖: "后台下载这 50 篇" / "等等，先查这个"

### 2.2 中断行为

用户发新消息时，当前前台任务自动转入后台。iOS 收到 `notification` 消息告知。

### 2.3 Timer 管理

Timer 作为 EventBus 的事件源，触发时投递 `timer_fired` 到 RunLoop：

| 类型 | 创建者 | 示例 |
|------|--------|------|
| 系统固定 | daemon 启动注册 | health_check (每20min), cleanup_logs (每天) |
| LLM 动态 | PlanGraph 调 create_timer tool | "每周一搜 adversarial attack" |
| 用户手动 | iOS 订阅 / CLI | 用户在 App 设置研究方向 |

---

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
| **VideoAgent** | 线性 Execute Graph | 8 节点 | parse_link→fetch_metadata→download→extract_audio→transcribe→summarize→analyze→notify；双策略下载 (yt-dlp + CloakBrowser 降级) |

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

## 4. 事件通信

### 4.1 EventBus vs Redis BRPOP — 互补不冲突

| 维度 | EventBus (asyncio.Queue) | Redis BRPOP |
|------|-------------------------|-------------|
| 通信范围 | 同一进程内 | 跨进程 (Celery Worker ↔ Daemon) |
| 延迟 | 纳秒级 | 毫秒级 |
| 持久化 | ❌ | ✅ Redis AOF |
| 适用场景 | PlanGraph↔工具状态↔iOS | Celery Worker→Daemon 进度 |

**两者汇入同一个 PriorityQueue**，RunLoop 统一分发。

### 4.2 统一工具调度

所有 Tool 调用分两类：

| 类型 | 条件 | 示例 | 方式 |
|------|------|------|------|
| 纯内存 | 无 IO/网络 | `read_file`, `get_user_preference`, `list_timers` | 直接调用 |
| Celery Task | 任何网络/磁盘/LLM | 其余全部 50+ 工具 | `celery.send_task()` + subscribe |

### 4.3 子 Agent → 主 Agent 通知通道

| 通道 | 方向 | 用途 |
|------|------|------|
| Redis `agent:events:{agent_id}` | 子→主 | Celery 进度/完成/错误 |
| TaskLogger JSONL | 子→主/iOS | 结构化审计日志 |
| EventBus | 子→主 (同进程) | PlanGraph 状态变化 |
| Redis Pub/Sub `agent:cmd:{agent_id}` | 主→子 | 暂停/取消指令 |

---

## 5. 工具系统

### 5.1 ToolRegistry（唯一注册中心）

```
src/paper_search/agent/tool_registry.py
    │
    ├── 注册: @register_tool / registry.register_direct()
    ├── 查询: get() / get_by_category() / get_by_tag()
    ├── 导出: to_langchain() / to_anthropic()
    │
    └── 每个工具标记:
        ├── location: "server" | "ios"
        ├── category: search | download | convert | index | analyze | export | manage | kb | subscription | system | network | memory | ios
        ├── is_idempotent: bool (重试安全)
        ├── is_long_running: bool (→ Celery)
        └── progress_report: bool (→ TaskLogger JSON 日志)
```

### 5.2 全系统工具清单

#### 5.2.1 主 Agent 工具（35 个）

**通用工具（6 个）**

| 工具 | 实现 | 功能 |
|------|------|------|
| `read_file` | Python `open()` | 读取文件（论文 MD、日志、配置） |
| `write_file` | Python `open()` | 写入文件（报告、BibTeX、配置） |
| `edit_file` | Python 字符串替换 | 精确编辑文件 |
| `glob_files` | Python `pathlib.glob` | 文件模式匹配 |
| `grep_content` | Python `re` / ripgrep | 文件内容搜索 |
| `bash_exec` | `subprocess.run` | Shell 命令（pip/apt/git/curl/ffmpeg） |

**网络工具（2 个）**

| 工具 | 实现 | 功能 |
|------|------|------|
| `web_search` | 火山引擎联网搜索 API（httpx） | 通用网页/图片搜索，500 次/月免费 |
| `web_fetch` | httpx + BeautifulSoup | 抓取单个 URL → Markdown |

> `web_search` 降级链：火山引擎 → web_fetch（httpx 直接抓取）→ bash_exec("curl ...")

**系统运维（10 个）**

| 工具 | 功能 |
|------|------|
| `service_start` | 启动 agent / celery_worker / redis |
| `service_stop` | 停止服务 |
| `service_status` | 查看运行状态 |
| `docker_compose_up` | Docker 一键启动 |
| `docker_compose_down` | Docker 停止 |
| `apt_install` | Ubuntu 系统依赖安装 |
| `pip_install` | Python 包安装 |
| `env_config` | 读写 .env 配置 |
| `log_view` | 查看 agent.log / task .jsonl |
| `health_check` | 全面健康检查（Providers + LLM + DB + Redis） |

**记忆管理（7 个）**

| 工具 | 内存层 | 功能 |
|------|--------|------|
| `search_memory` | LongTerm | 搜索历史对话（agent_conversations） |
| `summarize_memory` | ShortTerm→LongTerm | LLM 压缩旧对话为摘要 |
| `delete_memory` | ShortTerm | 删除冗余消息 |
| `extract_to_long_term` | ShortTerm→LongTerm | 提取持久知识 |
| `tag_memory` | ShortTerm | 给消息打标签 |
| `get_user_preference` | MetaMemory | 获取用户偏好 |
| `list_collections` | ChromaDB | 列出所有集合 |

**iOS 自动工具（9 个）**

> 一次授权后 Agent 可直接调用，无需用户交互。

| 工具 | 首次授权 | Agent 使用场景 |
|------|---------|---------------|
| `ios_file_read` | 文件访问 | 读取本地论文、报告 |
| `ios_file_write` | 文件访问 | 保存报告、BibTeX 到本地 |
| `ios_file_list` | 文件访问 | 列出已下载文件 |
| `ios_calendar_add` | 日历 | "提醒我周五前读完这篇" → 事件 |
| `ios_calendar_read` | 日历 | 了解空闲时段 |
| `ios_reminder_add` | 提醒事项 | 设置研究提醒 |
| `ios_notification_local` | 通知 | 每日论文推送、任务完成通知 |
| `ios_device_info` | 无 | 设备型号/系统版本/存储/网络状态 |
| `ios_location_get` | 位置 | 查会议地点距离、获取时区 |

> iOS 交互工具（share_sheet, open_url, pick_file, save_file_dialog, notification_permission）由 iOS 端自行实现，Agent 通过 `tool(ios, priority:2)` 请求触发，详参见 WebSocket 协议文档。

**直接查询（3 个）**

| 工具 | 功能 |
|------|------|
| `paper_status` | 查看项目/论文进度 |
| `list_sources` | 检查搜索来源可用性 |
| `get_paper_abstract` | 仅读摘要（省 token） |

#### 5.2.2 子 Agent 工具（19 个，去重）

| 工具 | 同步/异步 | 归属子 Agent |
|------|----------|-------------|
| `search_papers` | 同步 | IngestAgent, CitationChaseAgent |
| `batch_search` | 同步 | IngestAgent |
| `download_paper` | 异步(Celery) | IngestAgent, CitationChaseAgent |
| `convert_paper` | 异步(Celery) | IngestAgent, CitationChaseAgent |
| `index_paper` | 异步(Celery) | IngestAgent, CitationChaseAgent |
| `evaluate_papers` | 同步(LLM) | IngestAgent, CitationChaseAgent |
| `rank_papers` | 同步 | IngestAgent |
| `generate_survey` | 异步(Celery) | IngestAgent |
| `paper_export` | 同步 | IngestAgent |
| `paper_clean` | 同步 | IngestAgent |
| `citation_chase` | 同步 | CitationChaseAgent |
| `search_library` | 同步(ChromaDB) | RADQueryAgent, ClusteringAgent, TranslationAgent |
| `search_knowledge` | 同步(ChromaDB) | RADQueryAgent, TranslationAgent |
| `read_paper` | 同步(文件) | RADQueryAgent, CitationChaseAgent |
| `extract_knowledge` | 异步(Celery) | IngestAgent, RADQueryAgent, ClusteringAgent, TranslationAgent |
| `find_related` | 同步(ChromaDB) | RADQueryAgent |
| `discover_gaps` | 同步 | RADQueryAgent |
| `build_glossary` | 同步(LLM) | TranslationAgent |
| `translate_query` | 同步(LLM) | TranslationAgent |

### 5.3 Tool × Agent 分配矩阵

| Tool | 主Agent | Ingest | RADQuery | Cluster | CitationChase | History | Translation |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| search_papers | — | ✅ | — | — | ✅ | — | — |
| batch_search | — | ✅ | — | — | — | — | — |
| download_paper | — | ✅ | — | — | ✅ | — | — |
| convert_paper | — | ✅ | — | — | ✅ | — | — |
| index_paper | — | ✅ | — | — | ✅ | — | — |
| evaluate_papers | — | ✅ | — | — | ✅ | — | — |
| rank_papers | — | ✅ | — | — | — | — | — |
| generate_survey | — | ✅ | — | — | — | — | — |
| paper_export | — | ✅ | — | — | — | — | — |
| paper_status | ✅ | ✅ | — | — | — | — | — |
| paper_clean | — | ✅ | — | — | — | — | — |
| citation_chase | — | — | — | — | ✅ | — | — |
| list_sources | ✅ | ✅ | — | — | — | — | — |
| search_library | — | — | ✅ | ✅ | — | — | ✅ |
| search_knowledge | — | — | ✅ | — | — | — | ✅ |
| read_paper | — | — | ✅ | — | ✅ | — | — |
| get_paper_abstract | ✅ | — | ✅ | — | — | — | — |
| list_collections | ✅ | — | ✅ | ✅ | — | ✅ | — |
| extract_knowledge | — | ✅ | ✅ | ✅ | — | — | ✅ |
| find_related | — | — | ✅ | — | — | — | — |
| discover_gaps | — | — | ✅ | — | — | — | — |
| search_memory | ✅ | — | ✅ | — | — | ✅ | — |
| summarize_memory | ✅ | — | — | — | — | ✅ | — |
| delete_memory | ✅ | — | — | — | — | ✅ | — |
| extract_to_long_term | ✅ | — | — | — | — | ✅ | — |
| tag_memory | ✅ | — | — | — | — | ✅ | — |
| get_user_preference | ✅ | — | — | — | — | — | — |
| build_glossary | — | — | — | — | — | — | ✅ |
| translate_query | — | — | — | — | — | — | ✅ |
| web_search | ✅ | — | — | — | — | — | — |
| web_fetch | ✅ | — | — | — | — | — | — |
| read_file | ✅ | — | — | — | — | — | — |
| write_file | ✅ | — | — | — | — | — | — |
| edit_file | ✅ | — | — | — | — | — | — |
| glob_files | ✅ | — | — | — | — | — | — |
| grep_content | ✅ | — | — | — | — | — | — |
| bash_exec | ✅ | — | — | — | — | — | — |
| service_start | ✅ | — | — | — | — | — | — |
| service_stop | ✅ | — | — | — | — | — | — |
| service_status | ✅ | — | — | — | — | — | — |
| docker_compose_up | ✅ | — | — | — | — | — | — |
| docker_compose_down | ✅ | — | — | — | — | — | — |
| apt_install | ✅ | — | — | — | — | — | — |
| pip_install | ✅ | — | — | — | — | — | — |
| env_config | ✅ | — | — | — | — | — | — |
| log_view | ✅ | — | — | — | — | — | — |
| health_check | ✅ | — | — | — | — | — | — |
| ios_file_read | ✅ | — | — | — | — | — | — |
| ios_file_write | ✅ | — | — | — | — | — | — |
| ios_file_list | ✅ | — | — | — | — | — | — |
| ios_calendar_add | ✅ | — | — | — | — | — | — |
| ios_calendar_read | ✅ | — | — | — | — | — | — |
| ios_reminder_add | ✅ | — | — | — | — | — | — |
| ios_notification_local | ✅ | — | — | — | — | — | — |
| ios_device_info | ✅ | — | — | — | — | — | — |
| ios_location_get | ✅ | — | — | — | — | — | — |

### 5.4 子 Agent 执行模式

每个子 Agent 支持两种执行模式：

| 模式 | 说明 | 示例 |
|------|------|------|
| **Single Tool** | 主 Agent 调用子 Agent 执行单个 tool | "下载这篇论文" → IngestAgent.single(download_paper) |
| **ExecuteGraph** | 子 Agent 运行完整的 LangGraph StateGraph | "调研 Transformer 安全方向" → IngestAgent.ExecuteGraph() |

```
主 Agent 收到用户请求
  │
  ├── 简单操作 → 主 Agent 直接调用 tool
  │     ├── "检查论文入库进度" → paper_status
  │     ├── "复制这篇的引用"    → ios_clipboard_write
  │     └── "最近有什么 AI 安全新论文" → web_search
  │
  ├── Single Tool → 主 Agent → 子 Agent.single(tool)
  │     ├── "下载这篇论文" → IngestAgent.single(download_paper)
  │     └── "对抗攻击的英文关键词" → TranslationAgent.single(translate_query)
  │
  └── ExecuteGraph → 主 Agent → 子 Agent.ExecuteGraph()
        ├── "调研 Transformer 安全方向" → IngestAgent.ExecuteGraph()
        │     └── Celery Worker 后台执行，主 Agent 继续对话
        │     └── 进度通过 phase(execute) 实时推送 iOS
        ├── "分析这些论文的研究方向" → ClusteringAgent.ExecuteGraph()
        ├── "这篇论文引用了哪些后续工作" → CitationChaseAgent.ExecuteGraph()
        └── Agent 重启 → HistoryAgent.ExecuteGraph()
```

### 5.5 web_search 降级链

```
用户问题需要联网事实核查
  │
  ├── 1. 优先: 火山引擎 web_search（500 次/月免费）
  │     └── 成功 → 返回结构化结果
  │
  ├── 2. 降级: web_fetch（httpx 直接抓取目标 URL）
  │     └── 已知 URL 时直接获取，或火山额度耗尽后
  │
  └── 3. 兜底: bash_exec("curl ...")
        └── 以上均不可用时的最终降级
```

**火山引擎 API 配置**：

| 项目 | 值 |
|------|-----|
| 端点 | `https://open.feedcoopapi.com/search_api/web_search` |
| 认证 | `Bearer $WEB_SEARCH_API_KEY` |
| 免费额度 | 500 次/月 |
| 建议并发 | ≤ 5 |

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
| 18. ws_handler.py | WebSocket 7 大类消息 + 会话持久化 |
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

> 版本: v1.3 | Manifest启动协议、agent_id+session_id双层、Session内存隔离 | 2026-06-16
