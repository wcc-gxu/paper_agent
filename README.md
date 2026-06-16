# Paper Agent v3

> 个人 AI 科研助理 — 24/7 常驻后台，具备记忆、能主动发现知识、能自主决策的终身 Agent。

```
iOS 客户端 ←→ WebSocket + REST ←→ AgentRunLoop ←→ PlanGraph (主Agent)
                                 (事件驱动)      │
                                    │           ├── IngestAgent (7阶段入库)
                    ┌───────────────┼───────────┼── RADQueryAgent (规划中)
                    │               │           └── 其他子Agent (规划中)
                    ▼               ▼
          EventBus (纳秒)    Redis BRPOP (毫秒)
          (进程内)            (跨进程 Celery→Daemon)
                    │               │
                    └───────┬───────┘
                            ▼
              SQLite + ChromaDB + Celery Worker池
```

---

## 快速开始

### 前置依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | ≥ 3.11 | 运行环境 |
| Redis | ≥ 7.0 | Celery Broker + 事件总线 |
| Git | 任意 | 代码管理 |

### 1. 克隆安装

```bash
git clone <repo-url>
cd paper_agant
pip install -e ".[all]"
```

### 2. 配置

复制 `.env.example` 为 `.env`（或直接编辑项目根目录的 `.env`），填写 API Key：

```bash
# 必填
VOLCANO_API_KEY=ark-xxx          # 火山方舟 LLM
SEMANTIC_SCHOLAR_API_KEY=s2k-xxx # Semantic Scholar 搜索

# 可选（功能降级可用）
WEB_SEARCH_API_KEY=xxx           # 火山引擎联网搜索 (500次/月)
ELSEVIER_API_KEY=xxx             # ScienceDirect
IEEE_API_KEY=xxx                 # IEEE Xplore
```

### 3. 一键启动

```bash
python scripts/start.py
```

脚本会自动：
- 检查 Python / Redis / 依赖
- 初始化数据库
- 启动 Celery Worker
- 启动 FastAPI 服务
- 运行健康检测

### 4. 验证

```bash
# 健康检测（可独立运行）
python scripts/health_check.py

# 运行集成测试
pytest tests/ -v

# 访问 API 文档
open http://localhost:8000/docs
```

### 5. WebSocket 测试

```bash
# 启动服务器后运行
python tests/test_ws_client.py
```

---

## 架构

### 系统拓扑

```
iOS 客户端 (SwiftUI)
    │  WebSocket /ws/chat/{agent_id}/{session_id}
    │  REST API /api/*
    ▼
FastAPI (:8000)
    │
    ▼
AgentRunLoop (事件驱动主循环, PriorityQueue, 永不阻塞)
    ├── _ws_source (prio=0) — iOS 用户消息
    ├── _eventbus_source (prio=1~2) — 进程内状态
    ├── _redis_source (prio=1~2) — Celery 跨进程通知
    └── _timer_source (prio=3) — 定时任务
    │
    ▼
PlanGraph (主Agent 决策)
    ├── Parse → Clarify → Plan → Execute → Evaluate
    ├── Fire-and-Subscribe: 所有慢操作分发到 Celery
    └── 前台/后台自动判定 + 中断自动转入后台
    │
    ▼
Celery Worker 池 (共享, 多 Agent 复用)
    ├── 纯内存工具 (read_file 等) → 直接调用
    └── 其余全部 → celery.send_task() → subscribe 状态
    │
    ▼
存储层
    ├── SQLite (独立 agent.db per Agent)
    ├── ChromaDB — 6 个向量集合
    ├── Redis (共享) — Broker + agent:events:{id} + agent:cmd:{id}
    └── 文件系统 — PDF / Markdown / 日志
```

**多 Agent 部署**: 不同端口运行独立 daemon，共享 Redis + Celery Worker 池。

> 详见 [docs/development/agent-runloop.md](docs/development/agent-runloop.md)

### 子 Agent

| Agent | 类型 | 功能 |
|-------|------|------|
| **主 Agent** | PlanGraph | 意图解析 → 澄清 → 方案生成 → 委托执行 |
| **IngestAgent** | ExecuteGraph | 搜索 → 评估 → 下载 → 转换 → 索引 → 等级 → 综述 |
| RADQueryAgent | 规划中 | 知识库 RAG 问答 |
| ClusteringAgent | 规划中 | 研究方向聚类 |
| CitationChaseAgent | 规划中 | 引用追溯 |
| HistoryAgent | 规划中 | 历史消息处理 |
| TranslationAgent | 规划中 | 中英学术术语翻译 |

### 通信方式

| 通道 | 方向 | 技术 | 用途 |
|------|------|------|------|
| Redis List (agent:events) | 子→主 | LPUSH/BRPOP | 进度事件队列 |
| Redis Pub/Sub (agent:cmd:*) | 主→子 | PUBLISH/SUBSCRIBE | 指令下发 |
| TaskLogger JSONL | 子→主 | 文件/REST | 审计日志 |
| WebSocket | 主↔iOS | FastAPI WS | 实时对话 |

---

## 项目结构

```
paper_agant/
├── README.md
├── CLAUDE.md                 # Claude Code 参考卡
├── pyproject.toml            # 依赖 + CLI 入口
├── .env                      # API Key 配置
│
├── scripts/
│   ├── start.py              # 一键启动
│   └── health_check.py       # 综合健康检测
│
├── src/paper_search/
│   ├── agent/                # Agent 核心
│   │   ├── daemon.py         # 守护进程入口
│   │   ├── db.py             # SQLite 持久化 (AgentDB)
│   │   ├── memory.py         # 4 层记忆系统
│   │   ├── llm_client_v2.py  # LLM 客户端 (多供应商)
│   │   ├── prompt_optimizer.py # 3 阶段提示词优化
│   │   ├── tool_registry.py  # 56 个工具注册
│   │   │
│   │   ├── graphs/           # LangGraph 图
│   │   │   ├── plan_graph.py    # 主 Agent
│   │   │   └── ingest_graph.py  # IngestAgent
│   │   │
│   │   ├── sub_agent.py      # PipelineRunner 编排器
│   │   ├── task_logger.py    # JSON 日志
│   │   ├── reporter.py       # Celery→Agent 上报
│   │   ├── celery_app.py     # Celery 配置
│   │   ├── celery_tasks.py   # 4 个异步 Task
│   │   │
│   │   ├── chroma_store.py   # ChromaDB
│   │   ├── pdf_converter.py  # PDF→Markdown
│   │   ├── chunker.py        # 分块
│   │   ├── journal_ranker.py # 期刊分级
│   │   ├── knowledge.py      # 知识库
│   │   └── verifier.py       # 引用校验
│   │
│   ├── api/                  # FastAPI
│   │   ├── app.py            # WebSocket + REST
│   │   ├── routes.py         # REST 端点
│   │   ├── ws.py             # WebSocket 管理器
│   │   └── message_store.py  # 消息持久化
│   │
│   ├── providers/            # 7 个搜索来源
│   ├── downloaders/          # HTTP + Playwright 下载
│   ├── cli/                  # 13 个 CLI 命令
│   ├── mcp/                  # MCP Server
│   ├── engine.py             # 搜索引擎门面
│   ├── models.py             # Pydantic 数据模型
│   └── config.py             # 配置管理
│
├── tests/
│   ├── test_ws_client.py     # WS 协议测试
│   └── test_ingest_agent.py  # IngestAgent 测试
│
└── docs/                     # 设计文档
    ├── product/              # 产品规格
    ├── development/          # 架构/协议/技术选型
    └── reference/            # API 参考
```

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/product/product-spec.md](docs/product/product-spec.md) | 产品规格（15 个场景） |
| [docs/product/product-architecture-plan.md](docs/product/product-architecture-plan.md) | 产品与架构设计 |
| [docs/development/agent-runloop.md](docs/development/agent-runloop.md) | **AgentRunLoop** — 事件循环/统一调度/Timer/多Agent |
| [docs/development/architecture.md](docs/development/architecture.md) | 技术架构（拓扑/Agent/工具/代码组织） |
| [docs/development/websocket-protocol.md](docs/development/websocket-protocol.md) | WebSocket 协议 v6.0 |
| [docs/development/agent-manifest.md](docs/development/agent-manifest.md) | Agent Manifest 启动协议 |
| [docs/development/memory-system.md](docs/development/memory-system.md) | 4 层记忆系统 |
| [docs/development/tech-selection.md](docs/development/tech-selection.md) | 技术选型 |
| [docs/development/deployment.md](docs/development/deployment.md) | 部署与运维 |
| [docs/reference/volcengine-websearch-api.md](docs/reference/volcengine-websearch-api.md) | 火山引擎搜索 API |
| [CLAUDE.md](CLAUDE.md) | 代码参考卡 |

---

## 常用命令

```bash
# API Server
uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000 --reload

# Celery Worker
celery -A paper_search.agent.celery_app worker --loglevel=info --concurrency=4

# 论文搜索 CLI
paper-search "transformer attention" --sources arxiv,semantic_scholar --year-from 2023

# 运行测试
pytest tests/ -v

# 健康检测
python scripts/health_check.py
```

---

## License

MIT
