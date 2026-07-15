# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **架构文档索引**: [main-agent.md](docs/development/main-agent.md) — MainAgent LangGraph StateGraph · [anti-hallucination.md](docs/development/anti-hallucination.md) — 反幻觉策略 · [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议 · [memory-system.md](docs/development/memory-system.md) — LangGraph 三件套 + 双存储 · [api-reference.md](docs/development/api-reference.md) — API 参考文档
>
> **在线文档下载**: 服务启动后访问 `http://host:8000/paper/docs` 获取最新文档（自动同步文件时间戳，无需手动维护）

## 项目概述

Paper Agent v3 — 个人 AI 科研助理。输入研究方向 → 自动搜索/下载/阅读/综述/知识库沉淀。支持视频分享链接解析+下载+转写+LLM总结。

- **产品形态**: Python 后端 (FastAPI + WebSocket) + iOS 客户端
- **主 Agent**: MainAgent (LangGraph StateGraph 6 节点 + JSON Schema 强约束 + safety 双闸 + evaluate 5 出口)
- **消息链路**: Outbox 模式 (Redis List + PostgreSQL 持久化 + APNs 离线推送)
- **记忆系统**: LangGraph 三件套（Checkpointer 短期 / Store 长期 / 消息窗口管理）+ pgvector 向量存储
- **存储**: PostgreSQL+pgvector (业务表 + 向量) + Redis (队列) + 文件系统 (PDF/MD/Video)
- **定时任务**: Celery Beat (订阅检查 + health_check + cleanup_logs + consolidate_long_term + session_close_check)

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

### 主 Agent — v3.1 Fast Triage + ReAct Execute

```
WS 消息 → BRPOP agent:ws:{agent_id}
              ↓
       Fast Triage (flash, no thinking, tool_choice)
       → {chat, ops, research} 三维独立打分 + brief_reply
              ↓
    ┌─────────┼─────────┐
    ▼         ▼         ▼
  chat      ops    research
    │         │         │
    ▼         ▼         ▼
 inline   Execute   Intent Classify (flash, no thinking)
 reply   (ReAct)    → scenarios[]
  END    (自由工具)      │
                         ▼
                   Scenario Plan (pro, no thinking, tool_choice)
                   → {todos[], needs_clarify, danger_level}
                         │
                   needs_clarify? → ask → 用户回 → 重新 Plan
                         │
                         ▼
                   Execute (pro, thinking, ReAct loop)
                   → todo 内自由工具调用 (agent_* 子Agent + 普通tool)
                   → 同一轮 tool_use 并行，跨轮串行
                   → 每个 todo 结束 → checkpoint (flash)
                         │
                         ▼
                   Evaluate (flash, no thinking, tool_choice)
                   → {done, retry, ask_user, replan, fail}
```

**模型策略**：

| 节点类型 | 模型 | thinking | tool_choice |
|------|:---:|:---:|:---:|
| 结构化判断 (Triage/Intent/Plan/Eval/Checkpoint) | `deepseek-v4-flash` | 禁用 | 强制 |
| 生成类 (Execute ReAct/inline_reply) | `deepseek-v4-pro` | 开启 | 无 |

**Tool 命名规范**：子 Agent 以 `agent_` 前缀（`agent_search`, `agent_ingest`, `agent_survey`），普通 tool 无前缀。LLM 不区分两者——都是 tool_use。

**安全双闸**：(1) 入口 regex 同步秒过；(2) regex 命中后 LLM 异步并行二次确认，主流程不阻塞；(3) 每个 tool 调用前再过一次 regex 检测 arguments。fail-closed 纪律：LLM 不可用时一律拒答（详见 [anti-hallucination.md L4](docs/development/anti-hallucination.md)）。

**evaluate_completion 5 出口**：`done` / `retry_tools` / `ask_user` / `replan` / `fail`。总轮数硬上限 8 轮。`INTENT_ASK_THRESHOLD` 默认 0.6 控制 C3 灰区。

详细架构见: [main-agent.md](docs/development/main-agent.md)

### 17 个业务场景

`intent_classify` 把用户消息映射到 1~N 个 scenario_id（**支持复合意图**，2026-06-22 起）。LLM 返回 `scenarios: list[ScenarioMatch]`，每个场景独立判断 confidence：

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

主 Agent 的 LLM 调用全部通过 `llm_client_v2.chat_json(schema=PydanticClass)` 强制结构化输出（**Phase 2 修复**：补 Anthropic `tool_choice` 硬强制，从 ~90% 提升到 ≥99% 可靠性）：

- `SafetyResult` — safe + risk_kind ∈ {prompt_injection, jailbreak, pii_leak, other} + user_message（C1 安全前置）
- `IntentClassifyResult` — intent_kind + **scenarios: list[ScenarioMatch]**（C2 支持复合意图）+ overall_confidence + reasoning
- `ScenarioPlanResult` — summary + needs_clarification + needs_approval + permissions + requires_verification + **tools[] 一次性返回所有调用**
- `EvaluateCompletionResult` (v2) — satisfied + **next_action ∈ {done, retry_tools, ask_user, replan, fail}** + truth_confidence + final_message / needs_more_tools / ask_user_question / replan_hint

### 出站消息链路 — Outbox 模式

```
MainAgent → outbox_publish() ─┬─ PostgreSQL ws_messages (持久化)
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

**v10 capabilities**: WS handler 缓存 iOS 端上报的 `capabilities` 列表到 `_capabilities_cache`（`app.py:47` 模块级 dict），发 `tool/call` 前检查能力是否在列表中。

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
[iOS] GET /api/sessions/{session_id}/messages?since=<last_known_ts>
[Server] 返回去重后的最终状态消息列表（按 dedup_key DISTINCT ON）
         → REST 响应本身就是完成信号，无需 sync_complete
```

> **v10.2 变更**：WS sync 协议（sync_request/sync_complete）已移除，离线消息改为 REST API 拉取。详见 [websocket-protocol.md](docs/development/websocket-protocol.md)。

### 记忆系统 — LangGraph 三件套 + pgvector

详见 [docs/development/memory-system.md](docs/development/memory-system.md)

**对外口径（简历版）**：基于 LangGraph Checkpointer/Store 实现短期与长期记忆管理，配合 pgvector 向量存储完成 RAG 检索。

| 件 | 名称 | 作用域 | 实现 | 存什么 |
|:---:|---|---|---|---|
| ① | **Checkpointer** | thread-scoped（短期）| `AsyncPostgresSaver`（langgraph-checkpoint-postgres） | graph state（messages / phase / tool_results） |
| ② | **Store** | cross-thread（长期）| `AsyncPostgresStore`（langgraph-checkpoint-postgres + pgvector） | 用户偏好/画像/会话摘要/topic/策略/错误/知识 |
| ③ | **消息窗口管理** | 上下文窗口控制 | `trim_messages` + `SummarizationNode` + langmem | 滚动摘要 + 长期抽取 |

**Store 三层 8 个 namespace**（全部走 PostgreSQL + pgvector）：

```
(agent_id, "preferences")            ─ 用户偏好
(agent_id, "profile")                ─ 用户画像
(agent_id, "episodes", session_id)   ─ 会话摘要
(agent_id, "topics", topic_slug)     ─ 主题摘要（粗粒度按研究方向）
(agent_id, "strategies")             ─ 策略学习
(agent_id, "errors")                 ─ 错误模式
(agent_id, "knowledge", "papers")    ─ 论文元数据
(agent_id, "knowledge", "chunks")    ─ 论文 chunk
```

**三档压缩**：

| 档 | 触发 | 模式 | 关键参数 |
|---|---|---|---|
| 档 1 trim | 每次入口 | 无 LLM | 8k tokens / 保留最新 10 条 |
| 档 2 滚动摘要 | messages ≥ 30 OR tokens ≥ 16k | **hot path**（同步） | 单次≤100 条，超出按 token map-reduce 递归 |
| 档 3 长期抽取 | Beat 03:00 + session close | **background** | 7 天 lookback，写 Store 各 namespace |

**用户偏好更新双轨**：显式工具 `update_preference` + langmem 后台抽取。注入位置：system prompt 顶部 + Anthropic prompt caching（cache_control: ephemeral）。

`graph.compile(checkpointer=AsyncPostgresSaver, store=AsyncPostgresStore)` 自动注入。摘要后原 messages 归档到 `conversation_archive` 表，可回溯。

### 跨进程 resume — Checkpointer history

LangGraph Checkpointer 原生支持跨进程 resume，无需自研事件源 replay：

```
进程重启后 / iOS 重连 → 用同一 thread_id（= session_id）调
graph.aget_state(config={"configurable": {"thread_id": session_id}})
→ state.next 标识下一个待执行节点 → 自动续上
```

**v1 → v2 变化**：废弃 `agent_events` 表 + `_replay` + `_resume_from_state`，改用 Checkpointer 标准 history（3 张 langgraph 表 `checkpoints` / `checkpoint_blobs` / `checkpoint_writes`，与业务表同库）。反幻觉专用 telemetry 表 `hallucination_events` 保留作为项目级审计（[anti-hallucination.md §8.1](docs/development/anti-hallucination.md)），与 Checkpointer history 互补。

### 7 个子 Agent（v3 目标架构，Phase 2 实现）

当前代码层实际为 5 个 graph（见下方项目结构），v3 重构后目标为 7 个 Agent：

```
Literature Agent (graphs/literature_graph.py)   search→evaluate→download→convert→extract_metadata  [v3 新建，从 ingest_graph 拆分]
Knowledge Agent (graphs/knowledge_graph.py)     chunk→embed→dedup→rag_query                        [v3 新建，从 ingest_graph 拆分 + 整合 rad_query_graph]
Research Agent  (graphs/clustering_graph.py + citation_chase_graph.py)  cluster→label→detect + resolve→fetch→filter→summarize
Writing Agent   (graphs/writing_graph.py)       survey→template→citation_check→ai_flavor_check      [v3 新建]
Capture Agent   (graphs/video_graph.py 为其 tool) parse_link→download→transcribe→summarize           [v3 改名，原 Memory Agent]
Translation Agent (graphs/translation_graph.py) route→translate|build|enrich                        [保留]
Glossary Sub-Agent (graphs/glossary_graph.py)   collect→search→verify→evolve                         [v3 新增]
```

**v3 废弃/合并**：
- `ingest_graph.py` → 拆分为 Literature Agent + Knowledge Agent（Phase 2）
- `rad_query_graph.py` → 并入 Knowledge Agent 的 rag_query 节点（Phase 2）
- `history_graph.py` → 并入 Store episodes namespace（Phase 2）
- `video_graph.py` → 降级为 Capture Agent 的内部 tool（Phase 2）
- Memory Agent → 改名为 Capture Agent（v3 §3.4）

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
│   ├── daemon.py                   # 守护进程 + AgentBootstrap + graph.compile
│   ├── main_agent.py               # MainAgent 节点函数 + state 类型
│   ├── main_agent_prompts.py       # 17 scenario 定义 + 4 节点 Pydantic schema (Safety/Intent/Plan/Eval v2)
│   ├── agent_error.py              # AgentError 统一错误信封 + Redis Pub/Sub 上报
│   ├── summarizer.py               # 档 2 SummarizationNode + map-reduce + conversation_archive 归档
│   ├── message_trim.py             # 档 1 trim_messages 封装
│   ├── outbox.py                   # 出站消息双写 (PostgreSQL + Redis List)
│   ├── pgdb.py                     # PostgreSQL 持久化 (PostgresAgentDB)
│   ├── memory.py                   # MemoryManager + KnowledgeEntry (checkpointer/store 已迁 PG)
│   ├── llm_client_v2.py            # 多供应商 LLM（tool_choice 强制 + thinking 控制）
│   ├── tool_registry.py            # 73+ 工具 + update_preference 去重 + check_rag_health
│   ├── celery_app.py               # Celery 配置 + Beat (consolidate_long_term + session-close-check)
│   ├── celery_tasks.py             # 10 异步 Task + session_close + 订阅 + health_check + cleanup + 长期抽取
│   ├── reporter.py                 # Agent → 主 Agent 双通道上报 (Redis Pub/Sub agent:reports:{agent_id})
│   ├── task_logger.py              # 任务 JSON 日志
│   ├── sub_agent.py                # PipelineRunner 编排器
│   ├── verifier.py                 # 引用三步校验
│   ├── video_downloader.py         # yt-dlp 封装
│   ├── video_browser.py            # CloakBrowser 封装
│   ├── knowledge.py                # RAG 问答 + Cross-Encoder Rerank + rag_traces
│   ├── reranker.py                 # Cross-Encoder 重排序 (bge-reranker-v2-m3 via SiliconFlow)
│   ├── pgvector_store.py           # pgvector 向量存储 (PgVectorStore + message embeddings)
│   ├── pg_checkpointer.py          # LangGraph Checkpointer PostgreSQL 后端 (AsyncPostgresSaver)
│   ├── pg_store.py                 # LangGraph Store PostgreSQL 后端 (AsyncPostgresStore)
│   ├── pdf_converter.py            # PDF→Markdown
│   ├── chunker.py                  # Section-aware 分块
│   ├── journal_ranker.py           # CCF+SCI 期刊分级
│   │
│   └── graphs/                     # LangGraph StateGraph
│       ├── main_graph.py           # [Phase 2 新增] MainAgent StateGraph build + compile
│       ├── ingest_graph.py         # [v3 废弃] 拆分为 literature_graph + knowledge_graph（Phase 2）
│       ├── literature_graph.py     # [v3 新建] Literature Agent（Phase 2）
│       ├── knowledge_graph.py      # [v3 新建] Knowledge Agent（Phase 2）
│       ├── writing_graph.py        # [v3 新建] Writing Agent（Phase 2）
│       ├── glossary_graph.py       # [v3 新建] Glossary Sub-Agent（Phase 2）
│       ├── clustering_graph.py     # Research Agent 子 graph
│       ├── citation_chase_graph.py # Research Agent 子 graph
│       ├── translation_graph.py    # Translation Agent
│       ├── video_graph.py          # [v3] Capture Agent 的内部 tool
│       ├── rad_query_graph.py      # [v3 废弃] 并入 Knowledge Agent rag_query（Phase 2）
│       └── history_graph.py        # [v3 废弃] 并入 Store episodes（Phase 2）
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

## PostgreSQL 关键表（v3 Phase 1 迁移完成）

> Schema: [scripts/init_db.sql](scripts/init_db.sql) · 迁移脚本: [scripts/migrate_to_postgres.py](scripts/migrate_to_postgres.py)
>
> **当前状态**：全部模块已切 PostgreSQL（含 Checkpointer/Store/Celery/CLI/ToolRegistry）。SQLite 代码 (`db.py`/`chroma_store.py`/`checkpointer.py`/`store.py`) 已删除，`langgraph-checkpoint-sqlite`/`langgraph-store-sqlite`/`chromadb` 依赖已移除。`DATABASE_URL` 环境变量为必需项。
>
> **SQLite 兼容文件**（`agent.db`）：迁移源（`~/.paper_search/agent.db` ~1MB），历史保留，不再使用。

| 表 | 用途 | 迁移状态 |
|---|---|---|
| `users` | 多用户账户 + api_token | ✅ PG |
| `sessions` | 会话管理 | ✅ PG |
| `ws_messages` | 出站消息持久化 | ✅ PG |
| `message_embeddings` | 历史消息向量化召回 | ✅ pgvector (v10.2 新增) |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | LangGraph Checkpointer 3 表 | ✅ PG |
| `store_data` | LangGraph Store 数据 | ✅ PG |
| `conversation_archive` | 档 2 摘要后归档 | ✅ PG |
| `hallucination_events` | 反幻觉 telemetry | ✅ PG |
| `journal_ranks` | CCF+SCI 期刊分级 | ✅ PG |
| `paper_chunks` | 论文向量 chunk | ✅ pgvector |
| `glossary_embeddings` | 术语库向量 | ✅ pgvector |
| `session_summaries` | 会话摘要存储 | ✅ PG |
| `topic_embeddings` | 研究主题向量 | ✅ pgvector |
| `rag_traces` | RAG 检索可观测性 (延迟/错误) | ✅ PG (Phase 4 新增) |
| `session_scan_markers` | Celery Beat 增量扫描水位线 | ✅ PG (Phase 4 新增) |
| `device_tokens` | iOS APNs 设备 token | ✅ PG |
| `agent_tasks` / `task_steps` | Agent 任务跟踪 | ✅ PG |
| ~~`agent_events`~~ | ~~主 Agent 状态变更事件~~ | ❌ 废弃 (替换为 Checkpointer history) |
| ~~`task_checkpoints` / `knowledge_entries`~~ | ~~旧 MemGPT 表~~ | ❌ 废弃 |
| ~~`user_preferences` / `strategy_log` / `error_patterns`~~ | ~~MetaMemory 表~~ | ❌ 废弃 |

---

## Redis Key 清单

| Key | 类型 | 作用 |
|---|---|---|
| `agent:ws:{agent_id}` | List | iOS → Agent 入站消息队列 |
| `agent:ws:{agent_id}:parked` | List | _wait_ws_reply 暂存的不匹配消息（下轮重入） |
| `outbox:{agent_id}` | List | Agent → iOS 出站队列 (替代 agent:output Pub/Sub) |
| `agent:reports:{agent_id}` | Pub/Sub | 子 Agent → 主 Agent 进度 + lifecycle + AgentError 上报 |
| `agent:notifications` | Pub/Sub | Celery Beat 订阅检查 → API |
| `vec:cache:{user_id}:{md5(query)}` | String | 向量搜索结果缓存 (TTL 15min，可选) |

---

## Environment (.env)

| Variable | Purpose |
|---|---|
| `VOLCANO_API_KEY` | LLM (火山方舟) |
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar 1 req/s |
| `ELSEVIER_API_KEY` | ScienceDirect |
| `IEEE_API_KEY` | IEEE Xplore |
| `WEB_SEARCH_API_KEY` | 火山引擎联网搜索 (500次/月) |
| `DATABASE_URL` | PostgreSQL 连接 (必需，不再回退 SQLite) |
| `REDIS_URL` | Redis 连接 (默认 `redis://localhost:6379/0`) |
| `RERANK_API_KEY` | SiliconFlow Rerank API Key |
| `RERANK_BASE_URL` | Rerank 端点 (默认 `https://api.siliconflow.cn/v1/rerank`) |
| `RERANK_MODEL` | Rerank 模型 (默认 `BAAI/bge-reranker-v2-m3`) |
| `WHISPER_MODEL_SIZE` | Whisper 模型大小 (默认 `small`) |
| `CLOAKBROWSER_HEADLESS` | 浏览器无头模式 (默认 `1`) |
| `SUBSCRIPTION_CHECK_INTERVAL_MINUTES` | 订阅检查间隔 (默认 `60`) |
| `INTENT_ASK_THRESHOLD` | C3 灰区阈值 (默认 `0.6`)：所有 scenario.confidence < 此值时触发 ask_user |
| `DEBUG_PROTOCOL` | `=1` 时服务端推 `status{level:debug}` 消息（含 LLM thinking + rag_trace）；默认不推 |
| `API_KEY` | Bearer Token (REST API 认证，不设则禁用) |
| `APNS_KEY_PATH` / `APNS_KEY_ID` / `APNS_TEAM_ID` / `APNS_TOPIC` / `APNS_USE_SANDBOX` | APNs 推送 (Phase 1 骨架就位，aioapns 后补) |
| `VECTOR_CACHE_TTL` | 向量搜索结果 Redis 缓存 TTL 秒 (默认 `900` = 15min) |
| `VECTOR_SIMILARITY_THRESHOLD` | 向量召回相似度阈值 (默认 `0.75`) |

---

## Dependencies

```
# Core
langgraph>=0.6                          # StateGraph + Checkpointer + Store
langgraph-checkpoint-postgres>=2.0      # Checkpointer + Store PostgreSQL 后端
langmem>=0.0.10                         # create_memory_manager / SummarizationNode
fastapi>=0.110, uvicorn[standard]>=0.27
celery[redis]>=5.4, redis>=5.0
httpx>=0.27, pydantic>=2, requests>=2.28
arxiv>=2.3, biopython, metapub
pymupdf4llm
psycopg2-binary>=2.9                    # PostgreSQL 驱动
pgvector>=0.3                            # pgvector Python 客户端
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
| **v3 Phase 1: PostgreSQL+pgvector** | ✅ | 全部迁移完成；SQLite (db/chroma/checkpointer/store) 已删除，~80 处硬编码清零 |
| **Phase 4: Rerank + 去重 + 可观测性 + 会话关闭** | 🔶 | 代码完成(未提交)：reranker.py + rag_traces + 记忆/论文去重 + session_close_check Beat |
| **消息系统重构 (6 Phase)** | ✅ | WS sync 移除 + REST API + upsert + 向量召回 + SQLite 清零 (f02fde1) |
| **AgentError 统一错误报告** | ✅ | agent_error.py + Redis Pub/Sub + 全链路透传 + except:pass 清零 (8fcc999) |
| MainAgent v3.1 (Fast Triage + ReAct) | 📐 | 文档定型（[main-agent.md](docs/development/main-agent.md)），代码 Phase 2 |
| MainAgent v1.1 (自研 6 节点) | ✅ | 当前运行；v3.1 重构中 |
| LLM JSON Schema 强约束 | 🔶 | 当前 chat_json 用 tool 包装但缺 tool_choice 强制；Phase 2 修复 |
| 17 业务场景 | ✅ | 完整覆盖 + intent_classify 路由（支持复合意图） |
| 安全前置 (C1) | ✅ | regex 黑名单 + LLM 二次确认 |
| 安全双闸 (regex+异步 LLM+tool 前 regex) | 📐 | 文档定型，Phase 2 代码 |
| evaluate 5 出口 (done/retry/ask/replan/fail) | 📐 | 文档定型，Phase 2 代码 |
| 灰区 ask_user (C3) | ✅ | scenario.confidence < 0.6 时列候选场景 |
| Outbox 模式 (持久化+队列) | ✅ | 所有出站消息双写 + tool/plan 按 dedup_key upsert |
| outbox_poller (WS/APNs 分发) | ✅ | 每 agent 一个 poller |
| APNs 推送 | 🔶 | 骨架就位，aioapns 真实集成后补 |
| 上线历史同步 (v10.2) | ✅ | REST API `GET /api/sessions/{id}/messages` 替代 WS sync |
| **记忆系统 v2 (LangGraph 三件套 + PostgreSQL)** | ✅ | Checkpointer/Store 迁 PG；memory.py 保留 MemoryManager 业务逻辑 |
| 三档压缩 (trim/摘要/长期抽取) | ✅ | summarizer.py + message_trim.py + celery_tasks.py consolidate_long_term |
| Checkpointer (LangGraph 原生 resume) | ✅ | `AsyncPostgresSaver`，跨进程 resume 可用 |
| WebSocket 协议 | ✅ | v10.2：移除 sync，新增 tool_execution/plan_review/plan_todo_update |
| REST API | ✅ | `GET /api/sessions/{id}/messages` + JWT auth + 去重 + since 增量 |
| 消息向量召回 | ✅ | `message_embeddings` 表 + pgvector search_similar_messages + Redis 缓存 |
| Rerank (Cross-Encoder) | 🔶 | reranker.py (SiliconFlow bge-reranker-v2-m3)，替换 LLM rerank |
| RAG 可观测性 | 🔶 | rag_traces 表 + check_rag_health 工具 + DEBUG_PROTOCOL 推送 |
| 记忆去重 | 🔶 | update_preference: exact/high_sim skip + LLM merge |
| 7 子 Agent (LangGraph StateGraph) v2 | ✅ | 当前：Ingest/RADQuery/Cluster/CitationChase/History/Translation/Video + Knowledge/Literature/Writing/Glossary |
| 7 子 Agent v3 重构 | 📐 | 目标：Literature/Knowledge/Research/Writing/Capture/Translation/Glossary |
| Celery 任务 | ✅ | 10 task (含 session_close_check) + Beat 5 schedule |
| ToolRegistry | ✅ | 73+ 工具 (含 check_rag_health + update_preference 去重) |
| 反幻觉策略 (4 层主线) | 🔶 | L1/L4 已落地，L2/L3 Phase B/C |
| 文档 | ✅ | CLAUDE.md / main-agent.md / memory-system.md / websocket-protocol.md / anti-hallucination.md / api-reference.md |
| 测试 | 🔶 | 各模块有单元/集成测试；端到端 LLM 测试需配 API key |

> 状态图例：✅ 已落地 · 🔶 部分落地 · 📐 文档定型待 Phase 2 代码 · ❌ 废弃

### 待办

| # | 功能 | 工作量 | 备注 |
|---|------|:---:|------|
| 1 | **~~v3 Phase 1：基础设施重构~~** | ✅ 完成 | PostgreSQL+pgvector + SQLite 清零 + 多用户 + 冷启动 |
| 2 | v3 Phase 2：Agent 架构重构（ingest 拆分 + Writing/Glossary 新建 + Celery）| 2 周 | |
| 3 | v3 Phase 3：引用标记与验证（内外双通道 + 并行调度）| 2 周 | |
| 4 | v3 Phase 4：评估体系与收尾（检索质量 + 反幻觉 + Zotero）| 2 周 | |
| 5 | **提交 Phase 4 代码**（reranker + 去重 + rag_traces + session_close）| 1 天 | 代码已完成，待验证 + 提交 |
| 6 | aioapns 真实集成 + iOS 端注册流程 | 3-5 天 | 后端保留 WebSocket 协议兼容 |
| 7 | 可视化 (t-SNE/UMAP 研究方向图) | 3-5 天 | |
| 8 | Docker 部署 | 2-3 天 | |
| 9 | 测试覆盖率提升 | 持续 | |

> **注意**：Vue 前端迁移不在本项目范围内，由独立前端项目负责。后端保持 WebSocket 协议兼容，同时支持现有 iOS 客户端和未来 Vue 前端。
