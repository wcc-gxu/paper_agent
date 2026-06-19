# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **架构文档索引**: [agent-runloop.md](docs/development/agent-runloop.md) — AgentRunLoop · [architecture.md](docs/development/architecture.md) — 系统拓扑 · [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议 · [phase2-plan.md](docs/development/phase2-plan.md) — Phase 2 计划

## 项目概述

Paper Agent v3 — 个人 AI 科研助理。输入研究方向 → 自动搜索/下载/阅读/综述/知识库沉淀。

- **产品形态**: Python 后端 (FastAPI + WebSocket) + iOS 客户端
- **决策模型**: Plan-then-Execute (LangGraph StateGraph)
- **事件驱动**: AgentRunLoop + EventBus (PriorityQueue) + Redis BRPOP + Pub/Sub
- **存储**: SQLite (元数据) + ChromaDB (向量) + Redis (事件) + 文件系统 (PDF/MD)

## 启动方式

```bash
# 依赖
pip install -e ".[all]"

# 终端 1: Redis
redis-server

# 终端 2: Celery Worker
celery -A paper_search.agent.celery_app worker --loglevel=info --concurrency=4

# 终端 3: API Server (包含 PlanGraph)
uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000

# 终端 4: Agent Daemon (可选)
python -m paper_search.agent.daemon
```

---

## 核心架构

### 事件流 (AgentRunLoop)

```
WebSocket 消息  →  RunLoop(prio=0)  →  PlanGraph
Celery 完成     →  Redis BRPOP     →  RunLoop(prio=1)  →  PlanGraph
Celery 进度     →  Redis BRPOP     →  RunLoop(prio=2)  →  PlanGraph
子Agent 报告    →  Redis Pub/Sub   →  RunLoop(prio=1~2) →  PlanGraph
Timer 定时      →  RunLoop(prio=3)  →  PlanGraph
```

4 个事件源都通过 EventBus (asyncio.PriorityQueue) 汇入，RunLoop 逐个消费。同进程用 EventBus (纳秒级)，跨进程用 Redis (毫秒级)。

### PlanGraph (主 Agent)

```
START → parse_intent → (clarify? → await_clarify →) generate_plan
      → await_approval → await_permissions → execute_plan
      → overall_evaluate → (satisfied? → END | adjust → generate_plan)
```

- `parse_intent`: LLM 解析用户意图 (PromptOptimizer Stage 1)
- `clarify`: LLM 生成澄清问题 (Stage 2)
- `await_clarify` / `await_approval` / `await_permissions`: LangGraph interrupt 节点
- `generate_plan`: LLM 生成结构化方案 (Stage 3)
- `execute_plan`: 委托 ExecuteGraph 调度子 Agent
- `overall_evaluate`: LLM 评估执行结果

### ExecuteGraph → 7 种子 Agent

```
execute_graph.py (调度层)
    ├── IngestAgent        search→evaluate→download→convert→index→rank→[verify]→survey
    ├── RADQueryAgent      parse→route→search→evaluate(refine loop)→format
    ├── ClusteringAgent    load→cluster→label→visualize→detect
    ├── CitationChaseAgent resolve→check→fetch→filter→ingest→decide(loop)→summarize
    ├── HistoryAgent       analyze→generate_plan→archive→merge→skip→notify
    ├── TranslationAgent   route→translate|build|enrich
    └── VideoAgent         parse_link→fetch_metadata→download→extract_audio
                           →transcribe→summarize→analyze→notify
```

### 论文入库流水线 (IngestAgent)

```
search_papers  →  (papers in SQLite)
    → evaluate_papers → filter by relevance_score
        → download_paper  →  (updates pdf_path)
            → convert_paper  →  (updates markdown_path)
                → index_paper  →  (ChromaDB: 6 collections)
                    → [verify] → rank_papers → generate_survey
```

### 视频解析流水线 (VideoAgent)

```
parse_link  →  fetch_metadata (yt-dlp)  →  download_video  →  extract_audio (ffmpeg)
    →  transcribe (faster-whisper)  →  summarize (LLM)  →  analyze (LLM)  →  notify (SQLite)
```

- 长视频 (>10分钟): 跳过转录，基于标题/简介生成摘要
- 双策略下载: yt-dlp 直连 → 失败/缺 cookie → CloakBrowser 提取 cookie → yt-dlp 重试
- 支持 6 平台 URL 识别: 抖音(短链/长链/口令)/TikTok/B站/YouTube/小红书/快手
- Cookie 缓存: 30 分钟 TTL，同平台复用

### WebSocket 协议 (v7.0)

7 大类消息: `heartbeat` / `phase` / `thinking` / `message` / `tool` / `review` / `error`

握手: 首条消息必须是 `message(chat, seq=1)` → Server 返回 `phase(connected)`

---

## 项目结构

```
src/paper_search/
├── agent/                          # Agent 核心
│   ├── daemon.py                   # 守护进程 + AgentRunLoop
│   ├── event_bus.py                # 统一事件总线 (PriorityQueue + Redis源 + Timer源)
│   ├── db.py                       # SQLite 持久化 (AgentDB)
│   ├── memory.py                   # 4 层记忆系统
│   ├── llm_client_v2.py            # 多供应商 LLM (流式+重试+工具调用)
│   ├── llm_client.py               # V1 客户端 (向后兼容)
│   ├── prompt_optimizer.py         # 3 阶段提示词优化 (Parse/Clarify/Generate)
│   ├── tool_registry.py            # 56 个工具注册
│   ├── celery_app.py               # Celery 配置
│   ├── celery_tasks.py             # download/convert/index/survey 异步 Task
│   ├── reporter.py                 # Celery→Agent 双通道上报 (LPUSH + Pub/Sub)
│   ├── task_event_adapter.py       # 任务事件→WS 协议信封
│   ├── task_logger.py              # 任务 JSON 日志
│   ├── sub_agent.py                # PipelineRunner 编排器
│   ├── verifier.py                 # 引用三步校验 (格式/匹配/事实)
│   ├── video_downloader.py         # yt-dlp 封装 + URL解析 + CloakBrowser 降级
│   ├── video_browser.py            # CloakBrowser 封装 (链接解析 + cookie 导出)
│   ├── knowledge.py                # RAG 问答 + 知识提取 + 知识发现
│   ├── chroma_store.py             # ChromaDB 6 集合
│   ├── pdf_converter.py            # PDF→Markdown (pymupdf4llm)
│   ├── chunker.py                  # Section-aware 分块
│   ├── journal_ranker.py           # CCF+SCI 期刊分级
│   │
│   └── graphs/                     # LangGraph 图定义
│       ├── plan_graph.py           # 主 Agent (7 节点)
│       ├── execute_graph.py        # 子 Agent 调度层
│       ├── ingest_graph.py         # 论文入库 (8 节点, 含可选 verify)
│       ├── rad_query_graph.py      # RAG 问答 (5 节点)
│       ├── clustering_graph.py     # 聚类 (5 节点)
│       ├── citation_chase_graph.py # 引用追溯 (7 节点 + loop)
│       ├── history_graph.py        # 历史处理 (Plan+Execute)
│       ├── translation_graph.py    # 术语翻译 (工具型)
│       └── video_graph.py          # 视频解析 (8 节点: 链接→下载→转写→总结→分析)
│
├── api/                            # FastAPI 层
│   ├── app.py                      # 应用入口 + /ws/chat/{agent_id}/{session_id}
│   ├── routes.py                   # REST: /tasks, /papers, /projects, /knowledge, /ingest
│   ├── ws.py                       # WebSocket 连接管理器
│   ├── ws_handler.py               # WS 事件循环 (握手/重连回放)
│   ├── message_store.py            # 消息持久化 + 智能回放
│   ├── auth.py                     # Bearer Token 认证
│   └── middleware.py               # 速率限制
│
├── providers/                      # 7 个搜索来源
├── downloaders/                    # HTTP + Playwright 下载
├── cli/                            # 13 个 CLI 命令
├── engine.py                       # PaperSearchEngine 门面
├── models.py                       # Pydantic 数据模型
└── config.py                       # 配置管理
```

---

## 事件通信技术选型

| 场景 | 技术 | 理由 |
|------|------|------|
| **同进程** (Daemon 内部状态) | `asyncio.PriorityQueue` | 纳秒级，零序列化，无需外部依赖 |
| **跨进程** (Celery → Daemon 完成/错误) | Redis **BRPOP** (List) | Celery Worker 独立进程；阻塞等待无空转 |
| **跨进程** (子Agent 实时进度) | Redis **Pub/Sub** | 一对多广播，主Agent 按 task_id 订阅/取消 |
| **定时器** | `asyncio.sleep` | 简单可靠，无需 Celery Beat |
| **不选** Redis Streams | — | 场景是实时推送+消费即丢弃，Streams 过度设计 |
| **不选** Kafka/RabbitMQ | — | 单机部署，不需要消息中间件复杂度 |

4 个事件源全部向同一个 PriorityQueue 投递。RunLoop 逐个消费，不并发。低 prio 值先出队。

---

## Environment (.env)

| Variable | Purpose |
|----------|---------|
| `VOLCANO_API_KEY` | LLM (火山方舟) |
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar 1 req/s |
| `ELSEVIER_API_KEY` | ScienceDirect |
| `IEEE_API_KEY` | IEEE Xplore |
| `WEB_SEARCH_API_KEY` | 火山引擎联网搜索 (500次/月) |
| `REDIS_URL` | Redis 连接 (默认 `redis://localhost:6379/0`) |
| `WHISPER_MODEL_SIZE` | Whisper 模型大小 (默认 `small`) |
| `CLOAKBROWSER_HEADLESS` | 浏览器无头模式 (默认 `1`) |

---

## 子 Agent 独立日志

```
~/.paper_search/logs/
├── agent.log                        # 全局日志
└── sub_agents/
    ├── ingest/
    │   └── task-{YYYYMMDD}-{seq}.jsonl    # 按 task_id 分文件
    ├── citation_chase/
    │   └── task-{YYYYMMDD}-{seq}.jsonl
    ├── video/
    │   └── task-{YYYYMMDD}-{seq}.jsonl    # 视频解析日志
    └── ...
```

每条日志带 `task_id` + `agent_type` + `timestamp`，可按任务过滤。

---

## Dependencies

```
# Core
langgraph>=0.2.0, langgraph-checkpoint-sqlite>=1.0.0
fastapi>=0.110, uvicorn[standard]>=0.27
celery[redis]>=5.4, redis>=5.0
httpx>=0.27, pydantic>=2
arxiv>=2.3, biopython, metapub
pymupdf4llm, chromadb
python-dotenv, rich>=13

# Video (optional — pip install -e ".[video]")
yt-dlp>=2024.12           # 多平台视频下载 (1800+ 站点)
faster-whisper>=1.1.0     # 本地语音识别 (CTranslate2 加速)
cloakbrowser>=0.3         # 反检测浏览器 (cookie 提取 + 链接解析)
ffmpeg                    # 系统级依赖 (apt install ffmpeg)
```

Python >= 3.11
