# Agent Manifest — 智能体身份证与启动协议

> 定义 Agent 的身份、记忆位置、启动方式、迁移能力 | 2026-06-16

---

## 1. 设计目的

`agent_manifest.json` 是 Agent 的**身份证 + 启动说明书**。它不是记忆——记忆在 MemGPT 4 层系统中。Manifest 回答三个问题：

1. **我是谁？** — agent_id, 创建时间, 绑定的用户
2. **如何启动我？** — 入口点, Plan Graph 路径, checkpoint 后端
3. **我的记忆在哪？** — ShortTerm/MidTerm/LongTerm/MetaMemory 的存储位置

### 使用场景

| 场景 | 行为 |
|------|------|
| **首次启动** | manifest 不存在 → 创建主 Agent → 写入 manifest |
| **正常重启** | 读取 manifest → 从 checkpoint 恢复 Plan Graph → 加载 MemGPT |
| **服务器迁移** | 复制数据目录 → manifest 中的路径指向新位置 → 启动 |
| **多 Agent 扩展** | manifest 目录下新增 agent-002.json, agent-003.json |

---

## 2. Manifest 结构

### 完整 Schema

```json
{
  "manifest_version": "1.0",
  "agent": {
    "agent_id": "agent-001",
    "type": "main",
    "display_name": "我的科研助理",
    "created_at": "2026-06-16T08:00:00Z",
    "updated_at": "2026-06-16T15:30:00Z",
    "status": "active"
  },
  "owner": {
    "user_id": "user-default",
    "bound_since": "2026-06-16T08:00:00Z"
  },
  "runtime": {
    "plan_graph": {
      "module": "paper_search.agent.graphs.plan_graph",
      "class": "PlanGraph",
      "thread_id": "agent-001-plan"
    },
    "checkpoint": {
      "backend": "sqlite",
      "path": "~/.paper_search/agent.db",
      "table": "langgraph_checkpoints"
    },
    "event_bus": {
      "backend": "redis",
      "url": "redis://localhost:6379/0",
      "queue": "agent:events"
    },
    "llm": {
      "provider": "volcano",
      "model": "deepseek-v4-pro",
      "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3"
    }
  },
  "memory": {
    "short_term": {
      "max_tokens": 8000
    },
    "mid_term": {
      "db_path": "~/.paper_search/agent.db",
      "tables": {
        "checkpoints": "task_checkpoints",
        "tasks": "agent_tasks",
        "steps": "task_steps"
      }
    },
    "long_term": {
      "chroma_path": "~/.paper_search/chroma",
      "collections": [
        "papers_abstract",
        "papers_fulltext",
        "agent_conversations",
        "agent_knowledge",
        "agent_terminology",
        "agent_expressions"
      ],
      "sqlite_tables": ["knowledge_entries", "user_profile", "daily_expressions"]
    },
    "meta_memory": {
      "sqlite_tables": ["strategy_log", "error_patterns", "user_preferences"]
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
      {"type": "IngestAgent", "task_id": "task-20260616-001", "stage": "download", "progress": "12/22"}
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

### 字段说明

| 路径 | 类型 | 说明 |
|------|------|------|
### 默认值（MVP 写死）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `agent.agent_id` | `"agent-001"` | MVP 单 Agent |
| `agent.display_name` | `"我的科研助理"` | iOS 对话列表展示 |
| `owner.user_id` | `"user-default"` | MVP 单用户 |
| `sessions.default` | `"main"` | 默认会话 |
| `sessions.active[0]` | `"main"` | 首次创建 |
| main session 标题 | `"新对话"` | 第一条消息后 LLM 自动更新 |

| 路径 | 类型 | 说明 |
|------|------|------|
| `manifest_version` | string | Manifest 格式版本，用于向前兼容 |
| `agent.agent_id` | string | 全局唯一。格式: `agent-{序号}`。MVP = `agent-001` |
| `agent.type` | string | `main` = 主 Agent（用户对话入口）。MVP = `main` |
| `agent.status` | string | `active` / `paused` / `archived` |
| `owner.user_id` | string | 绑定用户。MVP = `user-default` |
| `runtime.plan_graph.thread_id` | string | LangGraph checkpoint 的 thread_id，重启恢复用 |
| `runtime.checkpoint.backend` | string | 当前仅 `sqlite`。未来可扩展 `postgres` |
| `runtime.llm` | object | 该 Agent 绑定的 LLM 配置（可不同于系统默认） |
| `memory.*` | object | 4 层记忆的存储位置。启动时 MemoryManager 据此加载 |
| `sessions` | object | 该 Agent 的所有 session。`default` = 新建对话时的默认 session |
| `sub_agents` | object | 活跃的子 Agent 列表。从 `agent_registry` 表同步 |
| `migration.compatible_agent_versions` | array | 哪些代码版本兼容此 manifest |


---

## 3. 启动协议

### 3.1 启动流程

```
系统启动 (daemon.py)
  │
  ├── 1. 扫描 data_dir / agent_manifest.json
  │     ├── 存在 → 进入恢复流程
  │     └── 不存在 → 进入创建流程
  │
  ├── 2. 恢复流程:
  │     ① 读取 manifest → 验证 manifest_version 兼容
  │     ② 初始化 runtime 组件:
  │        ├── AgentDB (manifest.memory.mid_term.db_path)
  │        ├── ChromaStoreV2 (manifest.memory.long_term.chroma_path)
  │        ├── LLM 客户端 (manifest.runtime.llm)
  │        ├── Redis 事件总线 (manifest.runtime.event_bus)
  │        └── Celery (复用 Redis broker)
  │     ③ 创建 MemoryManager → 加载 4 层记忆
  │     ④ 编译 LangGraph Plan Graph
  │        ├── 从 SQLite checkpoint (thread_id) 恢复 Plan Graph 状态
  │        └── 如果 checkpoint 存在 → 恢复到崩溃前节点
  │     ⑤ 从 agent_registry 表恢复活跃子 Agent
  │     ⑥ 检查 Redis 事件队列 → 回放未处理事件
  │     ⑦ 启动 FastAPI + WebSocket
  │     ⑧ 标记 agent.status = "active"
  │
  └── 3. 创建流程 (首次启动):
        ① 生成 agent_id = "agent-001"
        ② 选择 LLM 配置 (从环境变量)
        ③ 初始化空白数据库 (AgentDB schema)
        ④ 初始化空白 ChromaDB (创建 collections)
        ⑤ 初始化空白 MemoryManager
        ⑥ 编译 LangGraph Plan Graph (无 checkpoint，冷启动)
        ⑦ 注册 Celery tasks
        ⑧ 写入 agent_manifest.json
        ⑨ 启动 FastAPI + WebSocket
        ⑩ Agent 发送欢迎消息: "你好，我是你的科研助理。我可以帮你搜索论文、管理文献、生成综述。"
```

### 3.2 伪代码

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
    # Step 1: 加载数据库
    db = AgentDB(m.data.db_path)
    
    # Step 2: 加载 ChromaDB
    chroma = ChromaStoreV2(m.data.chroma_path)
    
    # Step 3: 加载 LLM
    llm = VolcanoChatModel(
        model=m.runtime.llm.model,
        base_url=m.runtime.llm.base_url
    )
    
    # Step 4: 加载记忆
    memory = MemoryManager(db, chroma)
    memory.short_term.max_tokens = m.memory.short_term.max_tokens
    
    # Step 5: 编译 Plan Graph
    plan_graph = PlanGraph(llm, memory, db, chroma)
    checkpointer = SqliteSaver.from_conn_string(m.runtime.checkpoint.path)
    graph = plan_graph.compile(checkpointer=checkpointer)
    
    # Step 6: 从 checkpoint 恢复
    state = await graph.aget_state(config={
        "configurable": {"thread_id": m.runtime.plan_graph.thread_id}
    })
    
    # Step 7: 恢复子 Agent
    active_subs = db.list_active_agents()
    for sub in active_subs:
        await restore_sub_agent(sub, db, llm, chroma)
    
    # Step 8: 标记活跃
    m.agent.status = "active"
    m.agent.updated_at = datetime.utcnow().isoformat()
    m.save()
    
    return m, graph, state


async def create_main_agent(data_dir: Path):
    agent_id = "agent-001"          # MVP 写死
    
    m = AgentManifest(
        manifest_version="1.0",
        agent=AgentInfo(
            agent_id=agent_id,
            type="main",
            display_name="我的科研助理",
            created_at=datetime.utcnow().isoformat(),
            status="active"
        ),
        sessions=SessionsInfo(
            default="main",
            active=["main"],
            archived=[]
        ),
        ...
    )
    
    # 初始化所有数据库表
    db = AgentDB(data_dir / "agent.db")
    db.initialize_schema()
    
    # 创建默认 main session
    db.create_session(
        session_id="main",
        agent_id="main",
        title="新对话",         # ← 第一条消息后 LLM 更新
        created_at=datetime.utcnow().isoformat()
    )
    
    # 初始化 ChromaDB collections
    chroma = ChromaStoreV2(data_dir / "chroma")
    for col in m.memory.long_term.collections:
        chroma.get_or_create_collection(col)
    
    m.save(data_dir / "agent_manifest.json")
    return m
```

---

## 4. 迁移协议

```
源服务器                        目标服务器
────────                        ────────
1. Agent 暂停
   m.agent.status = "paused"
   
2. 打包数据目录:
   tar -czf agent-001.tar.gz \
     ~/.paper_search/          \   # agent.db + chroma/ + logs/ + manifest
     ~/papers/                    # PDF + Markdown

3. 传输: scp agent-001.tar.gz target:~

4. 目标服务器解压:
   tar -xzf agent-001.tar.gz -C /home/user/

5. 启动 Agent:
   python -m paper_search.agent.daemon
   
6. Agent 读取 manifest →
   验证 data.base_dir 是否存在 →
   初始化组件 →
   从 checkpoint 恢复 →
   标记 status = "active" →
   就绪

注意:
  - manifest 中的路径如有变化，需手动修改或通过环境变量覆盖
  - Redis 事件队列中的消息不迁移（接受丢失）
  - ChromaDB 的 embedding function 需一致（或重建索引）
  - 迁移后第一次查询时验证 ChromaDB 集合完整性
```

---

## 5. 文件位置

```
~/.paper_search/
├── agent_manifest.json       # 主 Agent 身份证
├── manifests/                # 未来多 Agent 扩展
│   ├── agent-001.json
│   └── agent-002.json
├── agent.db                 # SQLite
├── chroma/                  # ChromaDB
└── logs/
    ├── agent.log
    └── tasks/
```

---

> 版本: v1.0 | 2026-06-16
