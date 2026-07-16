# Agent Manifest — 身份证与启动协议

> v2.0 | 2026-06-25 | 对齐 LangGraph 三件套
>
> 替代 v1.0（MemGPT 4 层记忆位置描述）。本文与 [memory-system.md](memory-system.md) v2.0 配套发布。

---

## §1 设计目的

`agent_manifest.json` 是 Agent 的**身份证 + 启动说明书**。它不是记忆——记忆在 LangGraph Checkpointer/Store 双系统中。Manifest 回答三个问题：

1. **我是谁？** — `agent_id`, 创建时间, 绑定的用户
2. **如何启动我？** — 入口点, StateGraph 模块, Checkpointer/Store 后端
3. **我的记忆在哪？** — Checkpointer / Store / conversation_archive 的存储位置

### 使用场景

| 场景 | 行为 |
|---|---|
| **首次启动** | manifest 不存在 → 创建主 Agent → 写入 manifest |
| **正常重启** | 读 manifest → `graph.compile(checkpointer=, store=)` → 按 thread_id 自动 resume |
| **服务器迁移** | 复制 SQLite + ChromaDB 数据目录 + manifest → 启动 |
| **多 Agent 扩展** | manifest 目录下新增 agent-002.json, agent-003.json |

---

## §2 Manifest 结构

### §2.1 完整 Schema（v2.0）

```json
{
  "manifest_version": "2.0",
  "agent": {
    "agent_id": "agent-001",
    "type": "main",
    "display_name": "我的科研助理",
    "created_at": "2026-06-25T08:00:00Z",
    "updated_at": "2026-06-25T15:30:00Z",
    "status": "active"
  },
  "owner": {
    "user_id": "user-default",
    "bound_since": "2026-06-25T08:00:00Z"
  },
  "runtime": {
    "main_agent": {
      "module": "paper_search.agent.graphs.main_graph",
      "build_func": "build_main_graph",
      "nodes": [
        "safety_regex_guard",
        "intent_classify",
        "inline_reply",
        "clarify",
        "scenario_plan",
        "execute_plan",
        "evaluate_completion"
      ],
      "iteration_limit": 8,
      "user_timeout_seconds": 1800
    },
    "checkpointer": {
      "backend": "async_sqlite",
      "conn_path": "~/.paper_search/agent.db",
      "tables": ["checkpoints", "checkpoint_blobs", "checkpoint_writes"]
    },
    "store": {
      "backend": "dual",
      "sqlite_path": "~/.paper_search/agent.db",
      "chroma_path": "~/.paper_search/chroma",
      "namespace_routes": {
        "preferences":  "sqlite",
        "profile":      "sqlite",
        "strategies":   "sqlite",
        "errors":       "sqlite",
        "episodes":     "chromadb",
        "topics":       "chromadb",
        "knowledge":    "chromadb"
      }
    },
    "outbox": {
      "backend": "redis",
      "url": "redis://localhost:6379/0",
      "key_pattern": "outbox:{agent_id}"
    },
    "llm": {
      "provider": "volcano",
      "model": "deepseek-v4-pro",
      "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
      "force_tool_choice": true,
      "enable_prompt_caching": true
    }
  },
  "memory": {
    "message_window": {
      "trim_max_tokens": 8000,
      "trim_keep_last": 10,
      "summary_trigger_count": 30,
      "summary_trigger_tokens": 16000,
      "summary_keep_recent": 10,
      "summary_batch_max": 100
    },
    "long_term": {
      "lookback_days": 7,
      "consolidate_beat_cron": "0 3 * * *",
      "namespaces": [
        "(agent_id, 'preferences')",
        "(agent_id, 'profile')",
        "(agent_id, 'episodes', session_id)",
        "(agent_id, 'topics', topic_slug)",
        "(agent_id, 'strategies')",
        "(agent_id, 'errors')",
        "(agent_id, 'knowledge', 'papers')",
        "(agent_id, 'knowledge', 'chunks')"
      ]
    },
    "archive": {
      "table": "conversation_archive",
      "retention_days": null
    }
  },
  "sessions": {
    "default": "main",
    "active": ["main", "cv-project"],
    "archived": []
  },
  "sub_agents": {
    "registry_table": "agent_registry",
    "active": [
      {
        "type": "IngestAgent",
        "task_id": "task-20260625-001",
        "stage": "download",
        "progress": "12/22",
        "ingest_params": {"task_kind": "screening", "keywords": ["transformer"]}
      }
    ]
  },
  "data": {
    "base_dir": "~/.paper_search",
    "papers_dir": "~/papers",
    "outputs_dir": "~/papers/outputs",
    "markdown_dir": "~/papers/markdown",
    "logs_dir": "~/.paper_search/logs",
    "tasks_log_dir": "~/.paper_search/logs/tasks"
  },
  "migration": {
    "last_migration_at": null,
    "compatible_agent_versions": ["3.0.0"],
    "data_checksum": "sha256:abc123def456..."
  }
}
```

### §2.2 字段说明

**runtime.checkpointer** — 短期记忆持久化后端

| 字段 | 说明 |
|---|---|
| `backend` | `async_sqlite` 单一支持；未来可扩展 `postgres` |
| `conn_path` | SQLite 文件路径，与业务表同库 |
| `tables` | LangGraph 标准 3 张表（自动创建） |

**runtime.store** — 长期记忆双后端

| 字段 | 说明 |
|---|---|
| `backend` | `dual` 表示 SQLite + ChromaDB 路由；`sqlite` / `chroma` 单后端调试用 |
| `namespace_routes` | namespace 第二层 kind → 后端的映射表（详见 [memory-system.md 附录 C](memory-system.md)） |

**runtime.llm**

| 字段 | 新增于 v2.0 | 说明 |
|---|:---:|---|
| `force_tool_choice` | ✓ | 是否强制 Anthropic `tool_choice` 硬约束（修复缺失） |
| `enable_prompt_caching` | ✓ | 是否启用 `cache_control: ephemeral`（user profile + prefs + summary） |

**memory.message_window** — 三档压缩参数（详见 [memory-system.md §3](memory-system.md)）

**memory.long_term** — 长期抽取调度

**memory.archive** — 摘要后归档表

### §2.3 默认值（MVP 写死）

| 字段 | 默认值 |
|---|---|
| `agent.agent_id` | `"agent-001"` |
| `agent.display_name` | `"我的科研助理"` |
| `owner.user_id` | `"user-default"` |
| `sessions.default` | `"main"` |
| main session 标题 | `"新对话"` |

---

## §3 启动协议

### §3.1 启动流程（v2.0）

```
系统启动 (daemon.py)
  │
  ├── 1. 扫描 data_dir / agent_manifest.json
  │     ├── 存在 → 恢复流程
  │     └── 不存在 → 创建流程
  │
  ├── 2. 恢复流程:
  │     ① 读取 manifest → 验证 manifest_version 兼容
  │     ② 初始化 runtime 组件:
  │        ├── AgentDB (manifest.runtime.checkpointer.conn_path)
  │        ├── AsyncSqliteSaver checkpointer (同库)
  │        ├── DualBackendStore (sqlite_store + chroma_store)
  │        ├── LLM 客户端 (manifest.runtime.llm)
  │        ├── Redis outbox + 入站队列
  │        └── Celery (复用 Redis broker)
  │     ③ build_main_graph().compile(checkpointer=, store=)
  │        ├── 注入 force_tool_choice / enable_prompt_caching
  │        ├── iteration_limit / user_timeout 校验
  │        └── interrupt_before 配置（如启用审批断点）
  │     ④ 启动 outbox_poller (每个 agent_id 一个协程)
  │     ⑤ 启动 FastAPI + WebSocket
  │     ⑥ 标记 agent.status = "active"
  │     ⑦ 已有 thread 的 resume：
  │        for session_id in active_sessions:
  │            config = {"configurable": {"thread_id": session_id}}
  │            state = await graph.aget_state(config)
  │            if state.next:
  │                # 自动从 Checkpointer 续上，无需自研 _replay
  │                ...
  │
  └── 3. 创建流程 (首次启动):
        ① 生成 agent_id = "agent-001"
        ② 选择 LLM 配置 (从环境变量)
        ③ 初始化 AgentDB schema (含 langgraph 标准 3 表 + conversation_archive)
        ④ 初始化 ChromaDB collections (knowledge/episodes/topics)
        ⑤ 初始化 Store 8 个 namespace
        ⑥ build_main_graph().compile()
        ⑦ 注册 Celery tasks（含 consolidate_long_term Beat）
        ⑧ 写入 agent_manifest.json
        ⑨ 启动 FastAPI + WebSocket
        ⑩ Agent 发送欢迎消息
```

### §3.2 伪代码

```python
async def bootstrap(data_dir: Path) -> AgentManifest:
    manifest_path = data_dir / "agent_manifest.json"

    if manifest_path.exists():
        manifest = AgentManifest.load(manifest_path)
        logger.info(f"Resuming agent: {manifest.agent.agent_id}")
        return await resume_agent(manifest)
    else:
        logger.info("First boot — creating main agent")
        manifest = await create_main_agent(data_dir)
        manifest.save(manifest_path)
        return manifest


async def resume_agent(m: AgentManifest):
    # Step 1: 加载数据库 + Checkpointer
    db = AgentDB(m.runtime.checkpointer.conn_path)
    checkpointer = AsyncSqliteSaver(conn=db.conn)
    
    # Step 2: 加载 Store 双后端
    sqlite_store = SqliteStore(db.conn)
    chroma_store = ChromaStore(m.runtime.store.chroma_path)
    store = DualBackendStore(sqlite_store, chroma_store)
    
    # Step 3: 加载 LLM
    llm = VolcanoChatModel(
        model=m.runtime.llm.model,
        base_url=m.runtime.llm.base_url,
        force_tool_choice=m.runtime.llm.force_tool_choice,
        enable_prompt_caching=m.runtime.llm.enable_prompt_caching,
    )
    
    # Step 4: 编译 main graph
    from paper_search.agent.graphs.main_graph import build_main_graph
    builder = build_main_graph(llm, store)
    graph = builder.compile(checkpointer=checkpointer, store=store)
    
    # Step 5: 启动外围
    await start_outbox_poller(m.agent.agent_id)
    
    # Step 6: 标记活跃
    m.agent.status = "active"
    m.agent.updated_at = datetime.utcnow().isoformat()
    m.save()
    
    return m, graph


async def create_main_agent(data_dir: Path):
    agent_id = "agent-001"
    
    m = AgentManifest(
        manifest_version="2.0",
        agent=AgentInfo(
            agent_id=agent_id,
            type="main",
            display_name="我的科研助理",
            created_at=datetime.utcnow().isoformat(),
            status="active",
        ),
        sessions=SessionsInfo(default="main", active=["main"], archived=[]),
        # ... 其余字段按 §2.1 schema 填默认值
    )
    
    # 初始化 AgentDB（含 langgraph 标准表 + conversation_archive）
    db = AgentDB(data_dir / "agent.db")
    db.initialize_schema()
    
    # 创建默认 main session
    db.create_session(
        session_id="main",
        agent_id="main",
        title="新对话",
        created_at=datetime.utcnow().isoformat(),
    )
    
    # 初始化 ChromaDB collections
    chroma_path = data_dir / "chroma"
    for col in ["knowledge_papers", "knowledge_chunks", "episodes", "topics"]:
        ChromaStore(chroma_path).get_or_create_collection(col)
    
    m.save(data_dir / "agent_manifest.json")
    return m
```

---

## §4 迁移协议

```
源服务器                        目标服务器
────────                        ────────
1. Agent 暂停
   m.agent.status = "paused"
   
2. 打包数据目录:
   tar -czf agent-001.tar.gz \
     ~/.paper_search/          \   # agent.db (含 Checkpointer 标准表) + chroma/ + logs/ + manifest
     ~/papers/                     # PDF + Markdown

3. 传输: scp agent-001.tar.gz target:~

4. 目标服务器解压:
   tar -xzf agent-001.tar.gz -C /home/user/

5. 启动 Agent:
   python -m paper_search.agent.daemon
   
6. Agent 读取 manifest →
   验证 manifest_version=2.0 →
   build_main_graph().compile(checkpointer=, store=) →
   按 sessions.active 中的 thread_id 调 graph.aget_state →
   有 state.next 的自动续上 →
   标记 status = "active" →
   就绪

注意:
  - manifest 中的路径如有变化，需手动改或通过环境变量覆盖
  - Redis 事件队列中的消息不迁移（接受丢失，由 outbox 持久化兜底）
  - ChromaDB 的 embedding function 需一致（或重建索引）
  - Checkpointer 标准表迁移后立即可用，无需重建
```

---

## §5 文件位置

```
~/.paper_search/
├── agent_manifest.json       # 主 Agent 身份证
├── manifests/                # 未来多 Agent 扩展
│   ├── agent-001.json
│   └── agent-002.json
├── agent.db                  # SQLite (业务表 + Checkpointer 标准 3 表 + conversation_archive)
├── chroma/                   # ChromaDB (knowledge/episodes/topics 集合)
└── logs/
    ├── agent.log
    └── tasks/
```

---

## §6 v1 → v2 字段迁移对照

| v1 字段 | v2 字段 | 说明 |
|---|---|---|
| `runtime.event_source` | `runtime.checkpointer` | 事件源 → Checkpointer history |
| `runtime.plan_graph.thread_id` | `sessions.active[*]` | thread_id 由 session_id 直接对应 |
| `memory.short_term.max_tokens` | `memory.message_window.summary_trigger_tokens` | 改为档 2 触发阈值 |
| `memory.mid_term.tables` | （废弃） | task_checkpoints 不再是记忆层 |
| `memory.long_term.collections` | `memory.long_term.namespaces` | 改用 Store namespace 表示 |
| `memory.long_term.sqlite_tables` | （并入 namespace_routes） | knowledge_entries 等并入 Store SQLite 后端 |
| `memory.meta_memory.sqlite_tables` | `memory.long_term.namespaces` 中 preferences/strategies/errors | 全归 Store |

---

> 版本: v2.0 | 2026-06-25 | 配套 [memory-system.md](memory-system.md) + [main-agent.md](main-agent.md) v2.0
