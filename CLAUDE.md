# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **架构文档索引**: [main-agent.md](docs/development/main-agent.md) — MainAgent 5 节点 · [architecture.md](docs/development/architecture.md) — 系统拓扑 · [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议 · [memory-system.md](docs/development/memory-system.md) — MemGPT 记忆

## 项目概述

Paper Agent v3 — 个人 AI 科研助理。输入研究方向 → 自动搜索/下载/阅读/综述/知识库沉淀。支持视频分享链接解析+下载+转写+LLM总结。

- **产品形态**: Python 后端 (FastAPI + WebSocket) + iOS 客户端
- **主 Agent**: MainAgent (5 节点显式状态机 + JSON Schema 强约束)
- **消息链路**: Outbox 模式 (Redis List + SQLite 持久化 + APNs 离线推送)
- **存储**: SQLite (元数据+消息+事件源) + ChromaDB (向量) + Redis (队列) + 文件系统 (PDF/MD/Video)
- **定时任务**: Celery Beat (订阅检查 + health_check + cleanup_logs)

## 启动方式

```bash
# 一键启动全部 5 个服务（跳过已在运行的）
bash scripts/start-all.sh

# 查看状态 / 停止
bash scripts/start-all.sh --status
bash scripts/start-all.sh --stop

# 手动逐个启动:
redis-server
celery -A paper_search.agent.celery_app worker --loglevel=info --concurrency=4
celery -A paper_search.agent.celery_app beat --loglevel=info
uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000
python -m paper_search.agent.daemon
```

### 依赖安装

```bash
pip install -e ".[all]"             # 全部
pip install -e ".[agent,celery,web]" # 不含视频
sudo apt install ffmpeg redis-server
```

---

## 核心架构

### 主 Agent — 5 节点状态机 (MainAgent)

```
WS 消息 → BRPOP agent:ws:{agent_id}
              ↓
       intent_classify (LLM #1, JSON Schema)
              ↓
       intent_kind ∈ {business, chat, meta, unsupported}
              ↓
        ┌─────┴─────┐
   business        chat/meta/unsupported
       ↓                 ↓
  scenario_plan      inline_reply (LLM 流式 thinking+text)
   (LLM #2)             ↓
       ↓             END (message/text)
   needs_clarify? → ask_user_question → (回答后再 plan)
       ↓
   needs_approval? → propose_plan → (用户批准)
       ↓
   execute_plan (并行调度 tools[] / sub_agent / ios_tool / ask_user)
       ↓
   evaluate_completion (LLM #4)
       ↓
   satisfied? → END | needs_more → execute_plan (最多 3 次迭代)
```

文件: [src/paper_search/agent/main_agent.py](src/paper_search/agent/main_agent.py) · prompts/schemas: [main_agent_prompts.py](src/paper_search/agent/main_agent_prompts.py)

### 17 个业务场景

`intent_classify` 把用户消息映射到 17 个 scenario_id 之一：

| ID | 场景 | 实现 |
|---|---|---|
| S1 | 文献调研/筛选 | ingest |
| S2 | 文献综述生成 (7阶段) | ingest |
| S3 | 每日前沿追踪 (订阅) | celery beat + subscription |
| S4 | 论文精读/提炼 | tool (extract_knowledge/read_paper) |
| S5 | 方法对比 | ingest |
| S6 | 研究空白分析 | clustering + discover_gaps |
| S7 | 进度查看 | tool (paper_status) |
| S8 | 聚类 + 全景图 | clustering |
| S9 | 引用追溯 | citation_chase |
| S10 | RAG 问答 (已入库) | rad_query |
| S11 | 批量搜索 | ingest x N |
| S12 | 学术翻译/术语库 | translation |
| S13 | 视频解析 | video |
| S14 | 导出/清理 | tool (paper_export/paper_clean) |
| S15 | iOS 自动化 | ios_tool |
| S16 | 运维操作 | tool (service/docker/pip) |
| S17 | 记忆操作 | tool (search_memory/extract_to_long_term) |

非业务请求（chat/meta/unsupported）走 `inline_reply` 直接 LLM 回复，不走任何子 Agent。

### LLM JSON Schema 强约束

主 Agent 的 LLM 调用全部通过 `llm_client_v2.chat_json(schema=PydanticClass)` 强制结构化输出：

- `IntentClassifyResult` — intent_kind + scenario_id + confidence + reasoning
- `ScenarioPlanResult` — summary + needs_clarification + needs_approval + permissions + **tools[] 一次性返回所有调用**
- `EvaluateCompletionResult` — satisfied + needs_more_tools + final_message

### 出站消息链路 — Outbox 模式

```
MainAgent → outbox_publish() ─┬─ SQLite ws_messages (持久化)
                               └─ Redis LPUSH outbox:{agent_id}
                                        ↓
                              outbox_poller (API 进程)
                                  BRPOP
                                  ↓
                          ┌───────────────┐
                          │ iOS 在线?     │
                          └──┬────────┬───┘
                            yes      no
                             ↓        ↓
                       ws.send_text   APNs (priority∈high/urgent)
                       mark_delivered
```

文件: [outbox.py](src/paper_search/agent/outbox.py) · [outbox_poller.py](src/paper_search/api/outbox_poller.py) · [apns_pusher.py](src/paper_search/api/apns_pusher.py) (Phase 1 骨架，aioapns 后补)

### 消息重要性

| priority_kind | 含义 | 在线 | 离线 |
|---|---|---|---|
| silent | 流式 thinking delta | WS 推 | 丢弃 |
| normal | tool 进度 | WS 推 + 持久化 | 持久化 |
| high | 任务完成/失败/plan卡片/澄清 | WS 推 + 持久化 | 持久化 + APNs |
| urgent | 错误/订阅推送 | WS 推 + 持久化 + APNs | 持久化 + APNs |

### iOS 上线同步

```
[iOS] WS connect → [Server] WS accept + outbox_poller 启动
[iOS] send {type: "sync_request", payload: {last_msg_id?: "..."}}
[Server] 拉 ws_messages 中本 session 未送达的消息 → 逐条 send_text
         → send {type: "sync_complete", payload: {synced_count: N}}
```

### 记忆系统 — MemGPT 4 层

详见 [docs/development/memory-system.md](docs/development/memory-system.md)

| 层 | 存储 | 用途 |
|---|---|---|
| ShortTerm | 进程内 deque (~8k tokens) | 当前会话滑动窗口 |
| MidTerm | SQLite (task_checkpoints) | 长任务进度快照 |
| LongTerm | SQLite + ChromaDB | 知识条目、对话摘要、用户画像 |
| MetaMemory | SQLite (user_preferences/strategy_log/error_patterns) | 偏好、策略学习 |

主 Agent 入口 `_build_history_context` 注入 `MetaMemory.profile + ShortTerm.get_context(8k)`；
LLM 主动调 `summarize_memory / extract_to_long_term / search_memory` 工具管理记忆。

### 事件源 Checkpoint

所有主 Agent 状态变更写 `agent_events` 表（15 种 event_type）。daemon 重启时：

```
_recover_pending_turns()
  → 找 turn_started 但无 turn_completed 的 correlation_id
  → _replay(events) 重建 state.phase / waiting_for
  → _resume_from_state():
       waiting_for ∈ {clarification, approval} → 推 high 提示给 iOS，标 turn_completed (用户回复时开新轮)
       running tools → 标失败 + error 推送
       其他 phase → 标 abandoned
```

### 7 个子 Agent（被 MainAgent 通过 `kind=sub_agent` 调度）

```
IngestAgent (graphs/ingest_graph.py)         search→evaluate→download→convert→index→rank→[verify]→survey
RADQueryAgent (graphs/rad_query_graph.py)    parse→route→search→evaluate(refine)→format
ClusteringAgent (graphs/clustering_graph.py)  load→cluster→label→visualize→detect
CitationChaseAgent (graphs/citation_chase_graph.py) resolve→check→fetch→filter→ingest→decide(loop)→summarize
HistoryAgent (graphs/history_graph.py)        analyze→generate_plan→archive→merge→skip→notify
TranslationAgent (graphs/translation_graph.py) route→translate|build|enrich
VideoAgent (graphs/video_graph.py)            parse_link→fetch_metadata→download→extract_audio→transcribe→summarize→analyze→notify
```

每个子 Agent 在收尾时调 `reporter.publish_lifecycle(task_id, "agent_done"|"agent_failed", ...)`，主 Agent 据此判定真正完成（解决了 v2 误把 per-paper status=done 当作 agent 完成的 P0 Bug）。

---

## 服务进程 (5 个)

| # | 服务 | 命令 | 端口 |
|---|------|------|:---:|
| 1 | Redis | `redis-server` | 6379 |
| 2 | Celery Worker | `celery -A paper_search.agent.celery_app worker --concurrency=4` | — |
| 3 | Celery Beat | `celery -A paper_search.agent.celery_app beat` | — |
| 4 | API Server | `uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000` | 8000 |
| 5 | Agent Daemon | `python -m paper_search.agent.daemon` | — |

---

## 项目结构

```
src/paper_search/
├── agent/                          # Agent 核心
│   ├── daemon.py                   # 守护进程 + AgentBootstrap + MainAgent 启动
│   ├── main_agent.py               # MainAgent 5 节点状态机（替代 v1/v2）
│   ├── main_agent_prompts.py       # 17 scenario 定义 + 3 节点 Pydantic schema
│   ├── outbox.py                   # 出站消息双写 (SQLite + Redis List)
│   ├── db.py                       # SQLite 持久化 (AgentDB)
│   ├── memory.py                   # MemGPT 4 层记忆 (ShortTerm/MidTerm/LongTerm/Meta)
│   ├── llm_client_v2.py            # 多供应商 LLM (含 chat_json 结构化输出)
│   ├── tool_registry.py            # 56 个工具注册
│   ├── celery_app.py               # Celery 配置 + Beat 定时调度
│   ├── celery_tasks.py             # 9 个异步 Task + 订阅 + health_check + cleanup_logs
│   ├── reporter.py                 # Celery → Agent 双通道上报 (LPUSH + Pub/Sub + lifecycle)
│   ├── task_logger.py              # 任务 JSON 日志
│   ├── sub_agent.py                # PipelineRunner 编排器
│   ├── verifier.py                 # 引用三步校验
│   ├── video_downloader.py         # yt-dlp 封装
│   ├── video_browser.py            # CloakBrowser 封装
│   ├── knowledge.py                # RAG 问答 + 知识提取
│   ├── chroma_store.py             # ChromaDB 6 集合
│   ├── pdf_converter.py            # PDF→Markdown
│   ├── chunker.py                  # Section-aware 分块
│   ├── journal_ranker.py           # CCF+SCI 期刊分级
│   │
│   └── graphs/                     # 7 子 Agent (LangGraph StateGraph)
│       ├── ingest_graph.py
│       ├── rad_query_graph.py
│       ├── clustering_graph.py
│       ├── citation_chase_graph.py
│       ├── history_graph.py
│       ├── translation_graph.py
│       └── video_graph.py
│
├── api/                            # FastAPI 层
│   ├── app.py                      # 应用入口 + WebSocket /ws/chat/{agent}/{session}
│   ├── routes.py                   # REST + /api/devices/register (APNs)
│   ├── ws.py                       # WebSocketManager (含 get_online_sessions)
│   ├── outbox_poller.py            # 消费 outbox List → WS / APNs 分发
│   ├── apns_pusher.py              # APNs 推送 (Phase 1 骨架)
│   ├── message_store.py            # 消息查询/回放（Phase 1 重写）
│   ├── auth.py                     # Bearer Token 认证
│   └── middleware.py               # 速率限制
│
├── providers/                      # 7 个搜索来源
├── downloaders/                    # HTTP + Playwright
├── cli/                            # 13 个 CLI 命令
├── engine.py                       # PaperSearchEngine 门面
├── models.py                       # Pydantic 模型
└── config.py                       # 配置管理
```

---

## SQLite 关键表（Phase 1 新增）

| 表 | 用途 | Phase |
|---|---|---|
| `ws_messages` | 出站消息持久化（msg_id/priority_kind/delivered_sessions/apns_sent_at） | Phase 1 扩展 |
| `device_tokens` | iOS APNs 设备 token | Phase 1 |
| `agent_events` | 主 Agent 状态变更事件（crash recovery 事件源） | Phase 4 |
| `task_checkpoints` | MidTerm 任务快照（MemoryManager） | 已有 |
| `knowledge_entries` | LongTerm 知识条目 | 已有 |
| `user_preferences` | MetaMemory 用户偏好 | 已有 |

---

## Redis Key 清单

| Key | 类型 | 作用 |
|---|---|---|
| `agent:ws:{agent_id}` | List | iOS → Agent 入站消息队列 |
| `agent:ws:{agent_id}:parked` | List | _wait_ws_reply 暂存的不匹配消息（下轮重入） |
| `outbox:{agent_id}` | List | **新** Agent → iOS 出站队列 (替代 agent:output Pub/Sub) |
| `agent:reports:{task_id}` | Pub/Sub | 子 Agent → 主 Agent 进度 + lifecycle |
| `agent:notifications` | Pub/Sub | Celery Beat 订阅检查 → API |

---

## Environment (.env)

| Variable | Purpose |
|---|---|
| `VOLCANO_API_KEY` | LLM (火山方舟) |
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar 1 req/s |
| `ELSEVIER_API_KEY` | ScienceDirect |
| `IEEE_API_KEY` | IEEE Xplore |
| `WEB_SEARCH_API_KEY` | 火山引擎联网搜索 (500次/月) |
| `REDIS_URL` | Redis 连接 (默认 `redis://localhost:6379/0`) |
| `WHISPER_MODEL_SIZE` | Whisper 模型大小 (默认 `small`) |
| `CLOAKBROWSER_HEADLESS` | 浏览器无头模式 (默认 `1`) |
| `SUBSCRIPTION_CHECK_INTERVAL_MINUTES` | 订阅检查间隔 (默认 `60`) |
| `API_KEY` | Bearer Token (REST API 认证，不设则禁用) |
| `APNS_KEY_PATH` / `APNS_KEY_ID` / `APNS_TEAM_ID` / `APNS_TOPIC` / `APNS_USE_SANDBOX` | APNs 推送 (Phase 1 骨架就位，aioapns 后补) |

---

## Dependencies

```
# Core
langgraph>=0.2.0              # 仅子 Agent 内部使用
fastapi>=0.110, uvicorn[standard]>=0.27
celery[redis]>=5.4, redis>=5.0
httpx>=0.27, pydantic>=2
arxiv>=2.3, biopython, metapub
pymupdf4llm, chromadb
python-dotenv, rich>=13

# Video (optional)
yt-dlp>=2024.12
faster-whisper>=1.1.0
cloakbrowser>=0.3
ffmpeg

# APNs (待集成)
# aioapns>=3.1
```

Python >= 3.11

---

## 当前项目进度

| 模块 | 状态 | 说明 |
|---|:---:|---|
| MainAgent (5 节点) | ✅ | 替代 v1 (PlanGraph) 和 v2 (AgentLoop) |
| LLM JSON Schema 强约束 | ✅ | IntentClassifyResult / ScenarioPlanResult / EvaluateCompletionResult |
| 17 业务场景 | ✅ | 完整覆盖 + intent_classify 路由 |
| Outbox 模式 (持久化+队列) | ✅ | 所有出站消息双写 |
| outbox_poller (WS/APNs 分发) | ✅ | 每 agent 一个 poller，按在线状态分发 |
| APNs 推送 | 🔶 | 骨架就位（device_tokens 表 + REST 端点 + APNsPusher），aioapns 真实集成后补 |
| 上线历史同步 | ✅ | sync_request/sync_complete 协议 |
| MemGPT 4 层记忆 | ✅ | 已接入 MainAgent；5 个记忆工具补齐真实实现 |
| 事件源 Checkpoint | ✅ | agent_events 表 + _replay + _resume_from_state |
| WebSocket 协议 v9.0 | ✅ | 无握手 + ping/pong + tool 统一 + 新增 propose_plan / sync_request |
| API Server (中继化) | ✅ | LPUSH→Agent + outbox_poller→WS |
| 7 种子 Agent | ✅ | Ingest/RADQuery/Cluster/CitationChase/History/Translation/Video |
| 子 Agent lifecycle 上报 | ✅ | sub_agent_task 收尾发 agent_done/agent_failed |
| Celery 任务 | ✅ | 9 个 task + Beat 三个 schedule (subscription/health_check/cleanup_logs) |
| ToolRegistry | ✅ | 56 个工具 |
| 文档 | ✅ | CLAUDE.md / main-agent.md 同步 |
| 测试 | 🔶 | 各模块有单元/集成测试；端到端 LLM 测试需配 API key |

### 待办

| # | 功能 | 工作量 |
|---|------|:---:|
| 1 | aioapns 真实集成 + iOS 端注册流程 | 3-5天 |
| 2 | 可视化 (t-SNE/UMAP 研究方向图前端) | 3-5天 |
| 3 | Docker 部署 | 2-3天 |
| 4 | 测试覆盖率提升 | 持续 |
