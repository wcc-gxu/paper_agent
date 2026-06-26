# Paper Agent v3 — 记忆系统设计

> v2.0 | 2026-06-25 | LangGraph 三件套 + ChromaDB+SQLite 双层存储
>
> 替代 v1.0（MemGPT 4 层 ShortTerm/MidTerm/LongTerm/MetaMemory），对齐 LangGraph 官方记忆架构标准。

---

## §1 设计目标

**对外口径（简历版）**：

> 基于 LangGraph Checkpointer/Store 实现短期与长期记忆管理，配合 ChromaDB+SQLite 双层存储完成 RAG 检索。

**三条核心目标**：

1. **跨 session resume** — 进程死了、用户重连，按 `thread_id` 立即续上对话状态，不依赖进程内 deque
2. **长期沉淀** — 用户偏好/画像/会话摘要/策略学习跨会话持久化，新对话开始时能"认识用户"
3. **可观测** — Checkpointer history 提供完整 state 时间线，反幻觉 telemetry 与之互补

---

## §2 LangGraph 三件套架构

```
┌──────────────────────────────────────────────────────────────┐
│ ① Checkpointer (短期 / thread-scoped)                         │
│    接口：BaseCheckpointSaver                                   │
│    实现：AsyncSqliteSaver(conn=AgentDB 同库)                   │
│    存什么：graph state（messages / phase / tool_results 等）   │
│    按 thread_id 索引（= WebSocket session_id）                 │
│    生命周期：thread_id 字符串被使用的时长（跨进程死活）           │
├──────────────────────────────────────────────────────────────┤
│ ② Store (长期 / cross-thread)                                 │
│    接口：BaseStore                                             │
│    实现：双后端                                                │
│      • SqliteStore  —— preferences/profile/strategies/errors  │
│      • ChromaStore  —— episodes/topics（带向量）/knowledge.*  │
│    按 namespace 路由：路由表在 store.py                         │
│    按 (agent_id, kind, entity_class?) 三层 8 个 kind 划分      │
├──────────────────────────────────────────────────────────────┤
│ ③ 消息窗口管理 (Message Window Management)                     │
│    档 1：trim_messages(max_tokens=8000, keep_last=10)         │
│    档 2：SummarizationNode（hot path）                         │
│    档 3：langmem.create_memory_manager（Celery Beat 03:00）   │
└──────────────────────────────────────────────────────────────┘
```

### §2.1 Checkpointer — 短期记忆

| 维度 | 说明 |
|---|---|
| **作用域** | thread-scoped — `thread_id` 相同的所有调用共享 |
| **不是** OS 线程、不是 Python `threading.Thread` | 是用户指定的字符串 ID |
| **生命周期** | 由 `thread_id` 字符串被使用的时长决定，不绑定进程 |
| **本项目 thread_id** | WebSocket `session_id`（每次 iOS 连接） |
| **存储后端** | `AsyncSqliteSaver`，指向 AgentDB 同一个 SQLite 文件 |
| **3 张标准表** | `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`（langgraph 标准 schema，与业务表同库） |
| **graph.compile 注入** | `graph.compile(checkpointer=checkpointer, store=store)` 自动绑定 |
| **配置传入** | 调用时 `config={"configurable": {"thread_id": session_id}}` |

**为什么 Checkpointer 不算"长期记忆"**：

它是**状态快照**，目的是 resume / time-travel，不是知识沉淀。即便 thread 永不删除，Checkpointer 也只保存某条 thread 的完整 state 历史，不会跨 thread 共享语义。

### §2.2 Store — 长期记忆

**namespace 设计：三层 8 个 kind**

```python
(agent_id, "preferences")            # 用户偏好
(agent_id, "profile")                # 用户画像
(agent_id, "episodes", session_id)   # 会话级摘要
(agent_id, "topics", topic_slug)     # 主题级摘要（粗粒度，按研究方向）
(agent_id, "strategies")             # 策略学习
(agent_id, "errors")                 # 错误模式
(agent_id, "knowledge", "papers")    # 论文元数据
(agent_id, "knowledge", "chunks")    # 论文 chunk 向量
```

**namespace 速查表**

| Namespace | 后端 | 写入时机 | 读取场景 | 保留策略 | 例子 |
|---|:---:|---|---|---|---|
| `(aid, "preferences")` | SQLite | 显式工具 `update_preference` / langmem 后台抽取 | MainAgent 入口注入 system prompt | 永久；显式 update | `{"tone": "concise", "reading_level": "deep"}` |
| `(aid, "profile")` | SQLite | langmem 后台抽取 | MainAgent 入口注入 system prompt | 永久；增量 update | `{"research_field": ["LLM", "多模态"], "institution": "..."}` |
| `(aid, "episodes", sid)` | ChromaDB | session close / 档 3 抽取 | LLM 调 `search_memory(query)` | 按 session 保留；可查 archived | 单次会话摘要 |
| `(aid, "topics", slug)` | ChromaDB | Beat 03:00 按粗粒度 topic 合并 | MainAgent 入口注入相关 topic 摘要 | 永久；按主题滚动合并 | `slug="transformer"` 下所有相关会话精华 |
| `(aid, "strategies")` | SQLite | execute_plan 收尾 | scenario_plan 调 `get_best_strategy` | 永久；effectiveness 增量 | `"S2 综述时先 S1 调研更稳"` |
| `(aid, "errors")` | SQLite | tool 异常 | scenario_plan 入口避坑 | 永久；重复模式自动聚合 | `{"tool": "arxiv_search", "pattern": "rate_limit_429"}` |
| `(aid, "knowledge", "papers")` | ChromaDB | IngestAgent index 节点 | RAG search_library | 永久 | 论文 metadata + abstract embedding |
| `(aid, "knowledge", "chunks")` | ChromaDB | IngestAgent index 节点 | RAG search_library 深度检索 | 永久 | section-aware chunks |

**为什么不按 17 场景做 namespace**：

- scenario 是 MainAgent 的**路由概念**，不是**记忆本体**
- 同一篇论文会被 S1/S5/S6/S10 多个场景复用，按 scenario 分会重复存
- scenario 作为 metadata filter 即可（不是 namespace 层级）

**为什么 topic 用粗粒度（研究方向）**：

- "transformer" / "钎焊" / "CV" 易跨会话汇总
- 细粒度任务级（"transformer-survey-2024"）namespace 膨胀且检索时要枚举
- 任务级信息进 metadata 字段而非 namespace

### §2.3 消息窗口管理 (Message Window Management)

不是单独的存储，是把 `messages` 列表压缩进上下文窗口的能力。**三档分级**详见 §3。

---

## §3 三档压缩与抽取

```
┌────────────────────────────────────────────────────────┐
│ 档 1 (实时 trim) — 每次入口砍                            │
│   触发：MainAgent 入口构建 prompt 之前                   │
│   工具：trim_messages(max_tokens=8000, keep_last=10)   │
│   成本：0（无 LLM）                                      │
│   语义：纯丢弃滑动窗口                                    │
└────────────────────────────────────────────────────────┘
                       ↓
┌────────────────────────────────────────────────────────┐
│ 档 2 (滚动摘要) — 中频压缩，hot path                     │
│   触发条件（任一）：                                      │
│     • messages 总数 ≥ 30                                │
│     • 累计 input_tokens ≥ 16000                         │
│   做法：                                                 │
│     1. 取最老的 K 条 messages（保留最新 10 条不动）       │
│     2. 若 K > 100 → 按 token map-reduce 递归摘要        │
│     3. LLM 总结成 1 条 SystemMessage："早期对话摘要..."  │
│     4. 用 RemoveMessage 删原 K 条，插入摘要               │
│     5. 原 K 条 messages 归档到 conversation_archive      │
│   成本：1 次 LLM（map-reduce 时数次，按 token 切分）      │
│   语义：保语义 + 压长度，仍 thread-scoped                 │
└────────────────────────────────────────────────────────┘
                       ↓
┌────────────────────────────────────────────────────────┐
│ 档 3 (长期抽取) — 低频沉淀，background                   │
│   触发：                                                 │
│     • Celery Beat 每天 03:00 跑                          │
│     • 或 session close 即时触发                          │
│   做法：                                                 │
│     LLM 从最近 7 天对话 + tool_results 抽取 4 类条目：    │
│       ① preferences — 用户偏好                          │
│       ② profile     — 用户画像                          │
│       ③ episodes    — 单次会话摘要（带 session_id）      │
│       ④ topics      — 粗粒度主题合并（按研究方向）        │
│       ⑤ strategies  — 工具组合有效性                     │
│   写入：对应 Store namespace                              │
│   成本：每天 1~3 次 LLM 调用                              │
│   语义：thread → cross-thread 转化的唯一路径              │
└────────────────────────────────────────────────────────┘
                       ↓
┌────────────────────────────────────────────────────────┐
│ 注入回路：下次会话开始时                                  │
│                                                          │
│   build_initial_state(thread_id, agent_id):              │
│     prefs   = store.search((agent_id, "preferences"))   │
│     profile = store.search((agent_id, "profile"))       │
│     topics  = store.search((agent_id, "topics"))        │
│     # 从 Checkpointer 加载 thread 历史                    │
│     state = checkpointer.get(config={thread_id})         │
│     messages = trim_messages(state.messages, 8000, 10)  │
│     return inject_to_system_prompt(prefs, profile,      │
│                                     topics, messages)    │
└────────────────────────────────────────────────────────┘
```

### §3.1 档 1 trim（参数固化）

| 参数 | 值 | env 名 |
|---|:---:|---|
| 最大 tokens | 8000 | `MSG_TRIM_MAX_TOKENS` |
| 保留最新条数 | 10 | `MSG_TRIM_KEEP_LAST` |
| 不可拆分类型 | ToolMessage / 紧邻的 ToolCall+ToolResult | 由 `trim_messages` 内置策略 |

### §3.2 档 2 滚动摘要（hot path）

| 参数 | 值 | env 名 |
|---|:---:|---|
| 触发 messages 阈值 | 30 | `SUMMARY_TRIGGER_COUNT` |
| 触发 token 阈值 | 16000 | `SUMMARY_TRIGGER_TOKENS` |
| 保留最新条数（不参与摘要） | 10 | `SUMMARY_KEEP_RECENT` |
| 单次摘要消息上限 | 100 | `SUMMARY_BATCH_MAX` |
| 超出上限策略 | map-reduce 递归（按 token 切分） | — |
| 摘要 LLM 模型 | 默认 sub 节点路由 | `MODEL_ROUTES["summary"]` |

**map-reduce 递归**（消息 > 100 条时）：

```
batch_summarize(messages):
    chunks = split_by_tokens(messages, max=8000)
    partial_summaries = [LLM(c) for c in chunks]
    return LLM("合并这些片段摘要: " + partial_summaries)
```

### §3.3 档 3 长期抽取（background）

| 参数 | 值 | env 名 |
|---|:---:|---|
| Beat 触发时间 | 每天 03:00 本地时间 | Celery Beat 配置 |
| Lookback 窗口 | 最近 7 天 | `LONGTERM_LOOKBACK_DAYS` |
| 即时触发 | WebSocket close + 30s 内无重连 | — |
| 抽取 LLM 模型 | 中级模型（temp=0.2） | `MODEL_ROUTES["extract"]` |
| 实现工具 | `langmem.create_memory_manager` | — |

**抽取产物**写入 4 类 namespace（preferences / profile / episodes / topics / strategies），见 §2.2 namespace 速查表。

---

## §4 与 RAG 的关系

**Store 与 RAG 共用 ChromaDB+SQLite 双层存储**，**按 namespace 严格隔离语义**：

| 维度 | Store（长期记忆） | RAG（论文知识库） |
|---|---|---|
| **存什么** | 用户偏好/画像/对话摘要/策略 | 论文 chunk / abstract / metadata / references |
| **谁写** | langmem 抽取器 / 显式工具 | IngestAgent.index 节点 |
| **谁读** | MainAgent 入口注入 prompt | LLM 调 `search_library / search_papers` 工具 |
| **namespace 例** | `(aid, "preferences")` | `(aid, "knowledge", "papers")` |
| **作用** | 让 Agent "认识你" | 让 Agent "回答有依据" |
| **后端选择** | SQLite（小数据）/ ChromaDB（带向量） | ChromaDB 主用 + SQLite 元数据 |

两者**不混用 namespace**，避免概念污染：偏好不会出现在 `knowledge` 下，论文 chunk 不会出现在 `episodes` 下。

---

## §5 用户偏好更新（双轨）

### §5.1 显式工具

`update_preference(key, value, source="user_explicit")` — LLM 在 inline_reply 或 evaluate_completion 节点检测到用户明确表态时调用：

```
用户："我只看 CCF-A 的论文"
LLM 调 update_preference("venues_filter", ["A+", "A"])
LLM 回复："已记录你的偏好：只看 CCF-A 及以上的论文。"
```

**关键**：必须给用户反馈"已记录"，否则用户不知道生效与否。

### §5.2 langmem 后台抽取

每天 03:00 Beat 跑 `langmem.create_memory_manager` 扫描最近 7 天对话，抽出"难以言表的"偏好（如风格、深度、回答格式）。

```python
manager = create_memory_manager(
    namespace=(agent_id, "preferences"),
    store=store,
    instructions="抽取用户对回答风格、深度、格式的偏好；忽略一次性请求。",
    schemas=[UserPreference],
)
await manager.update_from_messages(recent_messages)
```

### §5.3 写入冲突解决

- 显式优先：source="user_explicit" 永远覆盖 source="langmem_inferred"
- 同源更新：保留时间最近的；保留 confidence 高的（langmem 输出带 confidence 字段）
- 双源冲突：保留显式，langmem 推断进 `(aid, "preferences", "_inferred")` 备份 namespace 等待用户确认

---

## §6 注入与 Prompt Caching

### §6.1 注入位置（system prompt 顶部）

```python
system_blocks = [
    {
        "type": "text",
        "text": f"""# 用户画像
{profile_text}

# 用户偏好
{preferences_text}

# 最近相关 topic 摘要
{topics_text}

# 早期会话摘要
{rolling_summary or '（无）'}
""",
        "cache_control": {"type": "ephemeral"},  # ← 启用 caching
    },
    {
        "type": "text",
        "text": TASK_INSTRUCTION,                  # 静态任务指令
        "cache_control": {"type": "ephemeral"},
    },
]

messages = [
    {"role": "system", "content": system_blocks},
    *trim_messages(state.messages, max_tokens=8000, keep_last=10),
]
```

### §6.2 Prompt Caching 收益

| 维度 | 不开 caching | 开 caching |
|---|:---:|:---:|
| profile + prefs + topics + summary token | 每次全收费 | 命中后只收 10% |
| TTL | — | 5 分钟（活跃会话期间持续 hit） |
| 一天 100 轮对话成本 | 100× | 约 1×（首轮）+ 99×0.1 ≈ 10× 等效 |

### §6.3 Anthropic tool_choice 强制（重要修复）

当前 `llm_client_v2._chat_once` 缺失 `tool_choice` 参数，模型可能不调用预期 tool，结构化输出可靠性 ~90-95%。**Phase 2 修复**：

```python
# src/paper_search/agent/llm_client_v2.py _chat_once
if tools:
    payload["tools"] = self._to_anthropic_tools(tools)
    if force_tool:                                # 新参数
        payload["tool_choice"] = {"type": "tool", "name": tools[0].name}
```

火山方舟 Anthropic 兼容协议是否支持 `tool_choice` 强制需要实测；不支持则保留当前文本 JSON 兜底。

---

## §7 上线历史同步

### §7.1 流程

```
[iOS] WS connect → [Server] WS accept + outbox_poller 启动
[iOS] send {type: "sync_request", payload: {last_msg_id?: "..."}}
[Server] 拉 ws_messages 中本 session 未送达的消息 → 逐条 send_text
         → send {type: "sync_complete", payload: {synced_count: N}}
[Server] graph.aget_state(config={thread_id: session_id})
         → 自动从 Checkpointer resume 上下文，不需要任何 replay 代码
[MainAgent] 继续接收用户输入，state 已带历史 messages + waiting_for 等
```

### §7.2 thread_id 对应关系

| 概念 | 项目实体 |
|---|---|
| LangGraph `thread_id` | WebSocket `session_id` |
| 同 `thread_id` 跨进程 resume | 进程重启后 iOS 用同 session_id 重连 → 续上对话 |
| 跨 thread 共享 | Store 按 `agent_id` namespace 共享偏好/画像/topics |

---

## §8 SQLite 关键表清单

| 表 | 用途 | 来源 |
|---|---|---|
| `checkpoints` | LangGraph state 快照 | langgraph 标准 |
| `checkpoint_blobs` | state 中大对象（messages 等） | langgraph 标准 |
| `checkpoint_writes` | 单步写入记录 | langgraph 标准 |
| `store_data` | LangGraph Store 数据（SQLite 后端 namespace） | langgraph 标准 |
| **`conversation_archive`**（新增） | 档 2 摘要后归档的原始 messages | 项目新增 |
| `ws_messages` | 出站消息持久化（保留） | 项目已有 |
| `device_tokens` | iOS APNs 设备 token（保留） | 项目已有 |
| `knowledge_entries` | extract_knowledge 抽取的 method/contribution/limitation（保留） | 项目已有 |
| `journal_ranks` | CCF/SCI 期刊分级（保留） | 项目已有 |

**废弃表**（Phase 2 DB migration 移除）：

- `agent_events` — 由 Checkpointer history 替代
- `task_checkpoints` — 业务进度独立保留作 S7 "进度查看" 场景用，**不再作为 MidTerm 记忆层**

### §8.1 conversation_archive schema

```sql
CREATE TABLE conversation_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    archived_at TEXT NOT NULL,                    -- ISO8601
    summary_msg_id TEXT,                          -- 替换为哪条摘要消息（Checkpointer 中的 msg id）
    original_messages_json TEXT NOT NULL,         -- 原始 messages 列表 JSON
    original_count INTEGER NOT NULL,
    token_count INTEGER,
    reason TEXT                                   -- "trigger_count_30" / "trigger_tokens_16k"
);

CREATE INDEX idx_archive_thread ON conversation_archive(thread_id);
CREATE INDEX idx_archive_time   ON conversation_archive(archived_at);
```

---

## §9 迁移路径（从 v1 MemGPT 4 层到 v2 三件套）

### §9.1 概念字段对照

| v1 MemGPT 4 层 | v2 LangGraph 三件套 | 后端 |
|---|---|---|
| ShortTerm.deque(8k) | Checkpointer.state.messages + trim_messages | SQLite (checkpoints) |
| ShortTerm.summary | 档 2 SummarizationNode 输出 + conversation_archive | SQLite |
| MidTerm.task_checkpoints | **业务进度独立保留**（不再算记忆层） | SQLite (task_checkpoints) |
| LongTerm.knowledge_entries | Store namespace `(aid, "knowledge", "papers"/"chunks")` | ChromaDB + SQLite |
| LongTerm.conversation_summary | Store namespace `(aid, "episodes"/"topics")` | ChromaDB |
| MetaMemory.user_preferences | Store namespace `(aid, "preferences")` | SQLite |
| MetaMemory.profile | Store namespace `(aid, "profile")` | SQLite |
| MetaMemory.strategy_log | Store namespace `(aid, "strategies")` | SQLite |
| MetaMemory.error_patterns | Store namespace `(aid, "errors")` | SQLite |

### §9.2 代码改造步骤（Phase 2）

| # | 步骤 | 估算 |
|:---:|---|:---:|
| 1 | 新建 `src/paper_search/agent/checkpointer.py` — AsyncSqliteSaver 适配 + 同库连接 | 0.5 天 |
| 2 | 新建 `src/paper_search/agent/store.py` — 双后端 Store（SQLite + ChromaDB 路由） | 2 天 |
| 3 | 新建 `src/paper_search/agent/summarizer.py` — 档 2 SummarizationNode + map-reduce | 1 天 |
| 4 | 新建 `src/paper_search/agent/message_trim.py` — 档 1 trim_messages 封装 | 0.5 天 |
| 5 | 新建 Celery task `consolidate_long_term` — 档 3 langmem 抽取 | 1 天 |
| 6 | MainAgent 重写为 StateGraph + `graph.compile(checkpointer=..., store=...)` | 3-5 天 |
| 7 | `EvaluateCompletionResult` 加 `next_action` 5 出口 + 总轮数 8 守护 | 1 天 |
| 8 | safety_filter 异步并行 + tool 调用前 regex 二次检测 | 1-2 天 |
| 9 | `llm_client_v2._chat_once` 加 `tool_choice` 强制（3 行） | 0.5 天 |
| 10 | DB migration：`conversation_archive` 表 + 废弃 `agent_events` | 1 天 |
| 11 | IngestParams v2（21 字段）替换 IngestState；7 子 Agent 全部重写 | 5-7 天 |
| 12 | 注入 system prompt 头部 + prompt caching（cache_control） | 0.5 天 |
| 13 | 8 个 namespace 初始化 + `update_preference` 工具 + langmem 集成 | 1 天 |
| 14 | 删除 `memory.py` / `_build_history_context` / `_replay` / `_resume_from_state` | 0.5 天 |
| 15 | 测试：跨 session resume / 摘要触发 / 长期抽取 / namespace 检索 / IngestParams E2E | 3-5 天 |

总计约 20-30 人日，分多 PR 推进。

---

## 附录 A — IngestParams v2 schema（21 字段）

主 Agent → IngestAgent 的标准入参，**Phase 2 替换** `IngestState`：

```python
class IngestParams(BaseModel):
    """主 Agent → IngestAgent 的结构化入参（v2）。
    
    在 scenario_plan 节点澄清后一次性下传，子 Agent 全程不再向用户提问。
    """

    # ── 路由层 ─────────────────────────────────
    task_kind: Literal[
        "survey",          # S2 文献综述生成（7 阶段，触发 survey 节点）
        "screening",       # S1 文献调研/筛选
        "method_compare",  # S5 方法对比
        "gap_analysis",    # S6 研究空白分析
        "batch_search",    # S11 批量搜索
    ]
    scenario_ids: list[str]                              # ["S1"] / ["S5","S6"] 复合意图

    # ── 检索层 ─────────────────────────────────
    keywords: list[str]                                  # ["transformer", "attention"]
    research_field: Optional[str] = None                 # "CV" / "NLP" / "系统" / "安全"
    time_range: dict = Field(default_factory=dict)       # {"year_from": 2022, "year_to": 2026}
    venues_filter: Optional[list[str]] = None            # ["CVPR"] 或 ["A+", "A"]
    sources: list[str] = Field(default_factory=list)     # ["arxiv", "semantic_scholar", ...]
    max_results: int = 50
    must_have: list[str] = Field(default_factory=list)
    must_not: list[str] = Field(default_factory=list)

    # ── 处理层 ─────────────────────────────────
    download: bool = True
    convert_pdf: bool = True
    index_to_chroma: bool = True
    rank_by_journal: bool = True
    extract_knowledge: bool = False                      # 抽 method/contribution/limitation
    enable_verify: bool = False                          # 反幻觉验证

    # ── 输出层 ─────────────────────────────────
    output_format: Literal["list", "table", "survey_md"] = "list"
    survey_sections: Optional[list[str]] = None          # task_kind=survey 时用
    language: Literal["zh", "en"] = "zh"
    reading_level: Literal["skim", "deep"] = "skim"

    # ── 上下文 ─────────────────────────────────
    correlation_id: str
    session_id: str
    user_preferences: dict = Field(default_factory=dict) # 从 Store 注入的画像
```

**scenario_plan 澄清话术对照表**（缺字段触发 ask_user）：

| 缺字段 | 澄清问题 | 输入类型 |
|---|---|---|
| `keywords` | 你想搜什么关键词？ | text |
| `research_field` | 哪个研究方向？ | choice [CV/NLP/系统/安全/其他] |
| `time_range.year_from` | 时间范围？ | choice [近1年/近3年/近5年/不限] |
| `task_kind == "survey"` 且 `survey_sections is None` | 综述包含哪些章节？ | multi_choice [背景/方法对比/数据集/未来方向] |
| `max_results > 100` | 这次入库可能花 20 分钟+，确认继续？ | propose_plan |

---

## 附录 B — EvaluateCompletionResult v2 schema

```python
class EvaluateCompletionResult(BaseModel):
    """evaluate_completion 节点输出。Phase 2 新版本。"""

    satisfied: bool
    next_action: Literal[
        "done",          # → END，推 final_message
        "retry_tools",   # → execute_plan（带 needs_more_tools）
        "ask_user",      # → 推 ask_user_question → 等回复 → 回 evaluate
        "replan",        # → scenario_plan（带 replan_hint）
        "fail",          # → END，推 final_message 说明失败
    ]
    truth_confidence: float = Field(..., ge=0.0, le=1.0)  # 对工具结果可信度独立打分

    # 仅相关 action 才填
    needs_more_tools: list[ToolCallSpec] = []             # retry_tools
    ask_user_question: Optional[AskQuestion] = None       # ask_user
    replan_reason: Optional[str] = None                   # replan：说明为何重规划
    replan_hint: Optional[str] = None                     # replan：给 scenario_plan 的提示
    final_message: Optional[str] = None                   # done / fail
    reasoning: str                                        # 审计用 ≤300 字
```

**节点流转**：

```
evaluate_completion
    ├ done       → END (推 final_message high)
    ├ retry_tools → execute_plan
    ├ ask_user   → 推 ask + 等回复 → evaluate_completion
    ├ replan     → scenario_plan(带 replan_hint)
    └ fail       → END (推 fail final_message)

总轮数硬上限：8 轮（任何边的回流计入）
replan 不限次数（靠总轮数 8 兜底）
```

---

## 附录 C — Store 双后端路由表

```python
# src/paper_search/agent/store.py
NAMESPACE_BACKEND_ROUTES = {
    "preferences":  "sqlite",     # 小数据，无需向量
    "profile":      "sqlite",
    "strategies":   "sqlite",
    "errors":       "sqlite",
    "episodes":     "chromadb",   # 会话摘要带 embedding 检索
    "topics":       "chromadb",   # 主题级带 embedding 检索
    "knowledge":    "chromadb",   # 论文/chunk 大量向量
}

class DualBackendStore(BaseStore):
    """根据 namespace 第二层 kind 路由到对应后端。"""
    def __init__(self, sqlite_store: BaseStore, chroma_store: BaseStore):
        self.sqlite = sqlite_store
        self.chroma = chroma_store
    
    def _route(self, namespace: tuple[str, ...]) -> BaseStore:
        kind = namespace[1] if len(namespace) >= 2 else "preferences"
        backend = NAMESPACE_BACKEND_ROUTES.get(kind, "sqlite")
        return self.chroma if backend == "chromadb" else self.sqlite
    
    async def aput(self, namespace, key, value, index=None):
        return await self._route(namespace).aput(namespace, key, value, index)
    
    async def aget(self, namespace, key):
        return await self._route(namespace).aget(namespace, key)
    
    async def asearch(self, namespace_prefix, query=None, filter=None, limit=10):
        return await self._route(namespace_prefix).asearch(
            namespace_prefix, query=query, filter=filter, limit=limit
        )
```

---

## 附录 D — 依赖变更

```toml
# pyproject.toml additions (Phase 2)
[project.optional-dependencies]
agent = [
    "langgraph>=0.6",                       # 含 AsyncSqliteSaver
    "langgraph-checkpoint-sqlite>=2.0",     # SQLite checkpointer
    "langgraph-store-sqlite>=0.1",          # SQLite store（如启用）
    "langmem>=0.0.10",                      # create_memory_manager / SummarizationNode
    # ... (其他保留)
]
```

---

## 附录 E — 与反幻觉策略的互补

- `Checkpointer history` —— **graph state 快照**，给 resume / debug 用，按时间序列保留所有 node 输入输出
- `hallucination_events` 表（[anti-hallucination.md §8.1](anti-hallucination.md)） —— **反幻觉专用 telemetry**，记录 citation_verify / external_validate / groundedness / hallucination_review 的 verdict 与 truth_confidence

两者互补，**不替代**：前者是 LangGraph 通用观测，后者是项目级反幻觉审计。

---

## §10 与 v1 MemGPT 4 层的口径变化（给读者一段话说清）

旧文档把记忆按"热/温/冷/元"四层组织，分类依据是**时间尺度**。新文档按**作用域**重组：

- **thread-scoped**（一次会话内）→ Checkpointer
- **cross-thread**（用户级永久）→ Store（按 8 个 namespace 切分）
- **窗口管理**（压缩/裁剪/抽取）→ 消息窗口管理（三档）

带来的实际改善：

| 维度 | v1 MemGPT 4 层 | v2 三件套 |
|---|---|---|
| 进程死了，对话上下文 | ShortTerm 进程内 deque → 全丢 | Checkpointer SQLite → 续上 |
| 跨 thread 共享偏好 | 手工 `session_id` 过滤 | Store namespace 原生隔离 |
| 滑动窗口 | deque 自己实现 token 截断 | `trim_messages` 标准工具 |
| 长期沉淀 | 5 个自定义工具 + 散落表 | `langmem.create_memory_manager` 统一封装 |
| graph state 注入 | `_build_history_context` 手工拼装 | `graph.compile(checkpointer=, store=)` 自动 |
| Crash recovery | `agent_events` + `_replay` + `_resume_from_state` | Checkpointer `aget_state(thread_id)` 一行 |
| 跨进程 resume | 不支持 | 原生支持 |
| 标准化程度 | 自研接口 | LangGraph 官方标准 |

---

> 版本: v2.0 | 2026-06-25 | 替代 v1.0 (MemGPT 4 层)
