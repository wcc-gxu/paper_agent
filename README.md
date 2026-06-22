# Paper Agent v3

> 个人 AI 科研助理 — 输入研究方向，自动搜索→下载→阅读→综述→知识库沉淀。

**一句话：** 告诉它你想研究什么，它帮你搜论文、下 PDF、读全文、写综述、记住一切。

---

## 功能

### 核心场景

| 场景 | 描述 |
|------|------|
| **文献调研** | 输入研究方向 → 多源搜索 → LLM 评估相关性 → 筛选结果 |
| **综述生成** | 批量搜索 → 下载 PDF → 转换 Markdown → 向量索引 → 自动生成综述 |
| **论文精读** | 选定论文 → 提取核心贡献/方法/局限 → 结构化知识卡片 |
| **前沿追踪** | 订阅研究方向 → 每日自动搜索最新论文 → 主动推送 |

### 进阶能力

| 能力 | 说明 |
|------|------|
| **智能去重** | 跨来源合并重复论文（预印本 vs 正式出版） |
| **研究方向聚类** | 自动发现论文集合中的研究方向分布 |
| **新方向发现** | 识别不属于主流聚类但可能代表新兴方向的论文 |
| **RAG 问答** | 在已入库论文中自然语言提问，带引用标注 |
| **引用追溯** | 从一篇论文出发，沿引用网络发现相关工作 |
| **学术术语翻译** | 中英术语库，中文查询自动翻译为准确学术英文 |
| **24/7 常驻** | Agent 守护进程常驻后台，随时响应 |
| **视频解析** | 分享链接 → 下载 → 本地转写 → LLM 总结 + 深度分析 |

---

## 架构

```
iOS / WebUI
    │  WebSocket (实时对话) + REST (数据查询)
    ▼
FastAPI (:8000)                   ← 独立进程
    │  Redis
    ▼
AgentRunLoop                      ← 守护进程 (daemon.py)
    │  PriorityQueue 消费 4 事件源
    ├── WebSocket 消息 (prio=0)
    ├── Celery 结果   (prio=1~2)
    ├── Pub/Sub 报告  (prio=1~2)  
    └── Timer 定时    (prio=3)
    │
    ▼
PlanGraph (主 Agent 决策)
    Parse → Clarify → Plan → Permissions → Execute → Evaluate
    │
    ▼
ExecuteGraph (统一调度层)
    ├── IngestAgent       — 搜索入库 (7阶段)
    ├── RADQueryAgent     — RAG 问答
    ├── ClusteringAgent   — 研究方向聚类
    ├── CitationChaseAgent — 引用追溯
    ├── HistoryAgent      — 历史消息处理
    ├── TranslationAgent  — 术语翻译
    └── VideoAgent        — 视频链接解析+下载+转写+总结
    │
    ▼
存储: SQLite + ChromaDB + Redis + 文件系统
```

### 事件通信

```
同进程 (纳秒级):  EventBus (asyncio.PriorityQueue)
跨进程 (毫秒级):  Celery → Reporter → Redis BRPOP → EventBus
实时报告:         Celery → Redis Pub/Sub → SubAgentReportListener → EventBus
定时器:           asyncio.sleep → TimerEventSource → EventBus

所有事件汇入同一个 PriorityQueue，RunLoop 按优先级逐个消费。
```

---

## 快速开始

### 前置依赖

- Python ≥ 3.11
- Redis ≥ 7.0

### 1. 安装

```bash
git clone <repo-url> && cd paper_agant
pip install -e ".[all]"
```

### 2. 配置

编辑 `.env`：

```bash
# 必填
VOLCANO_API_KEY=ark-xxx              # LLM (火山方舟)
SEMANTIC_SCHOLAR_API_KEY=s2k-xxx     # 论文搜索

# 可选
REDIS_URL=redis://localhost:6379/0
```

### 3. 启动

```bash
# 终端 1: Redis
redis-server

# 终端 2: Celery Worker
celery -A paper_search.agent.celery_app worker --loglevel=info --concurrency=4

# 终端 3: API Server
uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000

# 终端 4: Agent Daemon (可选，API Server 内置了 PlanGraph)
python -m paper_search.agent.daemon
```

### 4. 验证

```bash
# 健康检查
curl http://localhost:8000/api/health

# CLI 搜索
paper-search "transformer attention mechanism" --sources arxiv,semantic_scholar
```

---

## 项目结构

```
paper_agant/
├── src/paper_search/
│   ├── agent/                    # Agent 核心
│   │   ├── daemon.py             # 守护进程 + AgentRunLoop
│   │   ├── event_bus.py          # 统一事件总线
│   │   ├── db.py                 # SQLite 持久化
│   │   ├── memory.py             # 4 层记忆系统
│   │   ├── llm_client_v2.py      # 多供应商 LLM 客户端
│   │   ├── prompt_optimizer.py   # 3 阶段提示词优化
│   │   ├── tool_registry.py      # 工具注册中心
│   │   ├── celery_app.py         # Celery 配置
│   │   ├── celery_tasks.py       # 异步任务
│   │   ├── reporter.py           # Celery→Agent 上报
│   │   ├── task_event_adapter.py # 任务事件→WS 协议
│   │   ├── task_logger.py        # JSON 日志
│   │   ├── sub_agent.py          # PipelineRunner
│   │   ├── video_downloader.py    # yt-dlp + URL 解析
│   │   ├── video_browser.py       # CloakBrowser cookie 提取
│   │   │
│   │   ├── graphs/               # LangGraph 图
│   │   │   ├── plan_graph.py         # 主 Agent
│   │   │   ├── execute_graph.py      # 子 Agent 统一调度
│   │   │   ├── ingest_graph.py       # 论文入库
│   │   │   ├── rad_query_graph.py    # RAG 问答
│   │   │   ├── clustering_graph.py   # 研究方向聚类
│   │   │   ├── citation_chase_graph.py # 引用追溯
│   │   │   ├── history_graph.py      # 历史消息处理
│   │   │   ├── translation_graph.py  # 术语翻译
│   │   │   └── video_graph.py        # 视频解析 (8节点)
│   │   │
│   │   ├── chroma_store.py       # ChromaDB
│   │   ├── pdf_converter.py      # PDF→Markdown
│   │   ├── chunker.py            # 分块
│   │   ├── journal_ranker.py     # 期刊分级
│   │   ├── knowledge.py          # 知识库 RAG
│   │   └── verifier.py           # 引用校验
│   │
│   ├── api/                      # FastAPI 层
│   │   ├── app.py                # 应用入口 + WS 端点
│   │   ├── routes.py             # REST 端点
│   │   ├── ws.py                 # WS 连接管理
│   │   ├── ws_handler.py         # WS 事件循环
│   │   ├── message_store.py      # 消息持久化 + 重连回放
│   │   ├── auth.py               # API Key 认证
│   │   └── middleware.py         # 速率限制
│   │
│   ├── providers/                # 7 个搜索来源
│   ├── downloaders/              # PDF 下载器
│   ├── cli/                      # 13 个 CLI 工具
│   ├── engine.py                 # 搜索引擎门面
│   ├── models.py                 # 数据模型
│   └── config.py                 # 配置管理
│
├── docs/                         # 设计文档
├── tests/                        # 测试
├── scripts/                      # 启动/健康检测脚本
├── pyproject.toml                # 依赖 + CLI 入口
└── .env                          # API Key 配置
```

---

## CLI 工具

| 命令 | 功能 |
|------|------|
| `paper-search` | 多源搜索 |
| `paper-download` | 下载 PDF |
| `paper-convert` | PDF→Markdown |
| `paper-index` | ChromaDB 索引 |
| `paper-evaluate` | LLM 相关性评估 |
| `paper-rank` | 期刊等级评定 |
| `paper-survey` | 生成综述报告 |
| `paper-export` | 导出 BibTeX/JSON |
| `paper-status` | 查看状态 |
| `paper-clean` | 清理数据 |

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [产品规格](docs/product/product-spec.md) | 15 个场景 |
| [产品架构设计](docs/product/product-architecture-plan.md) | 需求→产品→架构→技术→难点→开发计划 |
| [CLAUDE.md](CLAUDE.md) | 拓扑/Agent/工具/代码组织 |
| [AgentRunLoop](docs/development/agent-runloop.md) | 事件循环/调度/Timer |
| [WebSocket 协议](docs/development/websocket-protocol.md) | 7 大类 × 22 种子类消息 |
| [Phase 2 计划](docs/development/phase2-plan.md) | 24 问题 + 实施步骤 |
| [CLAUDE.md](CLAUDE.md) | 代码参考卡 |

---

## License

MIT
