# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **架构文档索引**: [main-agent.md](docs/development/main-agent.md) — MainAgent LangGraph StateGraph · [anti-hallucination.md](docs/development/anti-hallucination.md) — 反幻觉策略 · [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议 · [memory-system.md](docs/development/memory-system.md) — LangGraph 三件套 + 双存储

## 项目概述

Paper Agent v3 — 个人 AI 科研助理。输入研究方向 → 自动搜索/下载/阅读/综述/知识库沉淀。支持视频分享链接解析+下载+转写+LLM总结。

- **产品形态**: Python 后端 (FastAPI + WebSocket) + iOS 客户端
- **主 Agent**: MainAgent (LangGraph StateGraph 6 节点 + JSON Schema 强约束 + safety 双闸 + evaluate 5 出口)
- **消息链路**: Outbox 模式 (Redis List + SQLite 持久化 + APNs 离线推送)
- **记忆系统**: LangGraph 三件套（Checkpointer 短期 / Store 长期 / 消息窗口管理）+ ChromaDB+SQLite 双层存储
- **存储**: SQLite (业务表 + LangGraph Checkpointer 标准 3 表) + ChromaDB (向量) + Redis (队列) + 文件系统 (PDF/MD/Video)
- **定时任务**: Celery Beat (订阅检查 + health_check + cleanup_logs + consolidate_long_term)

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

### 主 Agent — LangGraph StateGraph 6 节点 (MainAgent v2)

```
WS 消息 → BRPOP agent:ws:{agent_id}
              ↓
       safety_regex_guard (同步 regex 快速通道, ~10ms)
              ↓
       safe? ─── no(高危) ──→ 推 high 拒答 → END
              ↓ yes / regex 命中需复查
              │  ↓ 异步并行启动 safety_llm task (asyncio.create_task)
              ↓
       intent_classify (LLM #1, IntentClassifyResult)
              ↓
       intent_kind ∈ {business, chat, meta, unsupported}
              ↓
       intent_kind == business?
              ↓
         ┌────┴────┐
        yes        no
         ↓          ↓
   maybe_clarify_low_confidence (C3)
   ├ 高置信 scenarios → 保留
   ├ 部分高 → 留高丢低
   └ 全低 → ask_user 列候选 + "都不是"
         ↓             ↓
   scenario_plan    inline_reply (LLM 流式 thinking+text)
   (LLM #2,            ↓
    ScenarioPlanResult)  END
   ├ 多 scenario 时逐场景生成子 plan → 合并 tools[]
   ├ needs_clarify? → ask_user_question
   ├ needs_approval? → propose_plan
   └ execute_plan (并行调度 tools[] / sub_agent / ios_tool / ask_user)
         ↓ (每个 tool 调用前 regex 二次检测；节点边界检 safety_llm task)
   evaluate_completion (LLM #4, EvaluateCompletionResult v2)
         ↓
   next_action ∈ {done, retry_tools, ask_user, replan, fail}
         ├ done       → publish 前最后 await safety_llm → END
         ├ retry_tools → execute_plan
         ├ ask_user   → 推 ask + 等回复 → evaluate_completion
         ├ replan     → scenario_plan (带 replan_hint)
         └ fail       → END (推 fail final_message)
         
   总轮数硬上限：8 轮（replan 不限次数，靠总轮数兜底）
```

文件: [src/paper_search/agent/main_agent.py](src/paper_search/agent/main_agent.py) · [graphs/main_graph.py](src/paper_search/agent/graphs/main_graph.py) (Phase 2) · prompts/schemas: [main_agent_prompts.py](src/paper_search/agent/main_agent_prompts.py)

**安全双闸**：(1) 入口 regex 同步秒过；(2) regex 命中后 LLM 异步并行二次确认，主流程不阻塞；(3) 每个 tool 调用前再过一次 regex 检测 arguments。fail-closed 纪律：LLM 不可用时一律拒答（详见 [anti-hallucination.md L4](docs/development/anti-hallucination.md)）。

**evaluate_completion 5 出口**：扩展 `next_action` 支持 `ask_user`（需用户判断）和 `replan`（方向不对需重规划）。`INTENT_ASK_THRESHOLD` 环境变量（默认 0.6）控制 C3 灰区阈值。

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

### 记忆系统 — LangGraph 三件套 + 双层存储

详见 [docs/development/memory-system.md](docs/development/memory-system.md)

**对外口径（简历版）**：基于 LangGraph Checkpointer/Store 实现短期与长期记忆管理，配合 ChromaDB+SQLite 双层存储完成 RAG 检索。

| 件 | 名称 | 作用域 | 实现 | 存什么 |
|:---:|---|---|---|---|
| ① | **Checkpointer** | thread-scoped（短期）| `AsyncSqliteSaver` 同库 | graph state（messages / phase / tool_results） |
| ② | **Store** | cross-thread（长期）| `DualBackendStore`（SQLite + ChromaDB 按 namespace 路由）| 用户偏好/画像/会话摘要/topic/策略/错误/知识 |
| ③ | **消息窗口管理** | 上下文窗口控制 | `trim_messages` + `SummarizationNode` + langmem | 滚动摘要 + 长期抽取 |

**Store 三层 8 个 namespace**：

```
(agent_id, "preferences")            ─ SQLite，用户偏好
(agent_id, "profile")                ─ SQLite，用户画像
(agent_id, "episodes", session_id)   ─ ChromaDB，会话摘要
(agent_id, "topics", topic_slug)     ─ ChromaDB，主题摘要（粗粒度按研究方向）
(agent_id, "strategies")             ─ SQLite，策略学习
(agent_id, "errors")                 ─ SQLite，错误模式
(agent_id, "knowledge", "papers")    ─ ChromaDB，论文元数据
(agent_id, "knowledge", "chunks")    ─ ChromaDB，论文 chunk
```

**三档压缩**：

| 档 | 触发 | 模式 | 关键参数 |
|---|---|---|---|
| 档 1 trim | 每次入口 | 无 LLM | 8k tokens / 保留最新 10 条 |
| 档 2 滚动摘要 | messages ≥ 30 OR tokens ≥ 16k | **hot path**（同步） | 单次≤100 条，超出按 token map-reduce 递归 |
| 档 3 长期抽取 | Beat 03:00 + session close | **background** | 7 天 lookback，写 Store 各 namespace |

**用户偏好更新双轨**：显式工具 `update_preference` + langmem 后台抽取。注入位置：system prompt 顶部 + Anthropic prompt caching（cache_control: ephemeral）。

`graph.compile(checkpointer=AsyncSqliteSaver, store=DualBackendStore)` 自动注入。摘要后原 messages 归档到 `conversation_archive` 表，可回溯。

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
│   ├── checkpointer.py             # [Phase 2 新增] AsyncSqliteSaver 适配
│   ├── store.py                    # [Phase 2 新增] DualBackendStore（SQLite + ChromaDB 路由）
│   ├── summarizer.py               # [Phase 2 新增] 档 2 SummarizationNode + map-reduce
│   ├── message_trim.py             # [Phase 2 新增] 档 1 trim_messages 封装
│   ├── outbox.py                   # 出站消息双写 (SQLite + Redis List)
│   ├── db.py                       # SQLite 持久化 (AgentDB)
│   ├── memory.py                   # [Phase 2 废弃] MemGPT 4 层（迁移到 checkpointer + store）
│   ├── llm_client_v2.py            # 多供应商 LLM（[Phase 2 修复] _chat_once 加 tool_choice）
│   ├── tool_registry.py            # 56 工具 + update_preference（新增）
│   ├── celery_app.py               # Celery 配置 + Beat (含 consolidate_long_term)
│   ├── celery_tasks.py             # 9 异步 Task + 订阅 + health_check + cleanup_logs + 长期抽取
│   ├── reporter.py                 # Celery → Agent 双通道上报
│   ├── task_logger.py              # 任务 JSON 日志
│   ├── sub_agent.py                # PipelineRunner 编排器
│   ├── verifier.py                 # 引用三步校验
│   ├── video_downloader.py         # yt-dlp 封装
│   ├── video_browser.py            # CloakBrowser 封装
│   ├── knowledge.py                # RAG 问答 + 知识提取
│   ├── chroma_store.py             # ChromaDB 集合管理
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

## SQLite 关键表

| 表 | 用途 | Phase |
|---|---|---|
| `ws_messages` | 出站消息持久化（msg_id/priority_kind/delivered_sessions/apns_sent_at） | Phase 1 |
| `device_tokens` | iOS APNs 设备 token | Phase 1 |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | LangGraph Checkpointer 标准 3 表（graph state 快照） | **Phase 2 新增** |
| `store_data` | LangGraph Store SQLite 后端数据（preferences/profile/strategies/errors namespace） | **Phase 2 新增** |
| `conversation_archive` | 档 2 摘要后归档的原始 messages（可回溯） | **Phase 2 新增** |
| `hallucination_events` | 反幻觉专用 telemetry（与 Checkpointer history 互补） | Phase 2 |
| `task_checkpoints` | 业务任务进度（**非 Checkpointer**，留作 S7 进度查询） | 已有 |
| `knowledge_entries` | extract_knowledge 抽取的 method/contribution/limitation | 已有 |
| `journal_ranks` | CCF+SCI 期刊分级 | 已有 |
| ~~`agent_events`~~ | ~~主 Agent 状态变更事件~~（**Phase 2 废弃**：由 Checkpointer history 替代） | ❌ Phase 2 删除 |
| ~~`user_preferences` / `strategy_log` / `error_patterns`~~ | ~~MetaMemory 表~~（**Phase 2 迁移**：并入 Store SQLite 后端 namespace） | ❌ Phase 2 迁移 |

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
| `INTENT_ASK_THRESHOLD` | C3 灰区阈值 (默认 `0.6`)：所有 scenario.confidence < 此值时触发 ask_user 让用户挑选场景 |
| `API_KEY` | Bearer Token (REST API 认证，不设则禁用) |
| `APNS_KEY_PATH` / `APNS_KEY_ID` / `APNS_TEAM_ID` / `APNS_TOPIC` / `APNS_USE_SANDBOX` | APNs 推送 (Phase 1 骨架就位，aioapns 后补) |

---

## Dependencies

```
# Core
langgraph>=0.6                          # StateGraph + AsyncSqliteSaver + Store
langgraph-checkpoint-sqlite>=2.0        # [Phase 2] Checkpointer SQLite 后端
langgraph-store-sqlite>=0.1             # [Phase 2] Store SQLite 后端（如启用）
langmem>=0.0.10                         # [Phase 2] create_memory_manager / SummarizationNode
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
| MainAgent v1.1 (自研 6 节点) | ✅ | 当前实现；Phase 2 重写为 LangGraph StateGraph |
| MainAgent v2.0 (LangGraph StateGraph) | 📐 | 文档定型（[main-agent.md](docs/development/main-agent.md)），代码 Phase 2 |
| LLM JSON Schema 强约束 | 🔶 | 当前 chat_json 用 tool 包装但缺 tool_choice 强制；Phase 2 修复 |
| 17 业务场景 | ✅ | 完整覆盖 + intent_classify 路由（支持复合意图） |
| 安全前置 (C1) | ✅ | regex 黑名单 + LLM 二次确认 |
| 安全双闸 (regex+异步 LLM+tool 前 regex) | 📐 | 文档定型，Phase 2 代码 |
| evaluate 5 出口 (done/retry/ask/replan/fail) | 📐 | 文档定型（[EvaluateCompletionResult v2](docs/development/memory-system.md)），Phase 2 代码 |
| 灰区 ask_user (C3) | ✅ | scenario.confidence < 0.6 时列候选场景 |
| Outbox 模式 (持久化+队列) | ✅ | 所有出站消息双写 |
| outbox_poller (WS/APNs 分发) | ✅ | 每 agent 一个 poller |
| APNs 推送 | 🔶 | 骨架就位，aioapns 真实集成后补 |
| 上线历史同步 | ✅ | sync_request/sync_complete 协议（v2 直接调 Checkpointer aget_state） |
| **记忆系统 v1 (MemGPT 4 层)** | 🔶 | 当前实现；Phase 2 完整迁移到三件套 |
| **记忆系统 v2 (LangGraph 三件套 + 双存储)** | 📐 | 文档定型（[memory-system.md](docs/development/memory-system.md)），代码 Phase 2 |
| 8 个 namespace + Store 双后端路由 | 📐 | 文档定型，Phase 2 代码 |
| 三档压缩 (trim/摘要/长期抽取) | 📐 | 文档定型，Phase 2 代码 |
| Prompt Caching (Anthropic ephemeral) | 📐 | 文档定型，Phase 2 代码 |
| 事件源 Checkpoint v1 (agent_events 自研) | ✅ | 当前；Phase 2 替换为 Checkpointer history |
| Checkpointer v2 (LangGraph 原生 resume) | 📐 | 文档定型，Phase 2 代码 |
| WebSocket 协议 v10.0 | ✅ | 5 卡片 / 12 消息 / 内部编排不可见 |
| API Server (中继化) | ✅ | LPUSH→Agent + outbox_poller→WS |
| 7 子 Agent (LangGraph StateGraph) v2 | ✅ | 当前：Ingest/RADQuery/Cluster/CitationChase/History/Translation/Video |
| 7 子 Agent v3 重构 | 📐 | 目标：Literature/Knowledge/Research/Writing/Capture/Translation/Glossary（[v3 方案](docs/product/智驭研_重构方案_v3.md)） |
| 子 Agent lifecycle 上报 | ✅ | sub_agent_task 收尾发 agent_done/agent_failed |
| IngestParams v2 (21 字段) | 📐 | 文档定型（[memory-system.md 附录 A](docs/development/memory-system.md)），Phase 2 代码 |
| 7 子 Agent v2 重写 (吃 IngestParams) | 📐 | 文档定型，Phase 2 代码 |
| Celery 任务 | ✅ | 9 task + Beat（Phase 2 加 consolidate_long_term）|
| ToolRegistry | ✅ | 56 工具（Phase 2 加 update_preference）|
| 反幻觉策略 (4 层主线) | 🔶 | L1/L4 已落地，L2/L3 Phase B/C |
| 文档 | ✅ | CLAUDE.md / main-agent.md / memory-system.md / agent-manifest.md / anti-hallucination.md 全套对齐 |
| 测试 | 🔶 | 各模块有单元/集成测试；端到端 LLM 测试需配 API key |

> 状态图例：✅ 已落地 · 🔶 部分落地 · 📐 文档定型待 Phase 2 代码 · ❌ 废弃

### 待办

| # | 功能 | 工作量 | 备注 |
|---|------|:---:|------|
| 1 | **v3 Phase 1：基础设施重构**（PostgreSQL+pgvector + 多用户 + 冷启动 + 数据迁移）| 2 周 | 当前优先，详见 [backend-dev-plan](docs/development/backend-development-plan.md) |
| 2 | v3 Phase 2：Agent 架构重构（ingest 拆分 + Writing/Glossary 新建 + Celery）| 2 周 | |
| 3 | v3 Phase 3：引用标记与验证（内外双通道 + 并行调度）| 2 周 | |
| 4 | v3 Phase 4：评估体系与收尾（检索质量 + 反幻觉 + Zotero）| 2 周 | |
| 5 | aioapns 真实集成 + iOS 端注册流程 | 3-5 天 | 后端保留 WebSocket 协议兼容 |
| 6 | 可视化 (t-SNE/UMAP 研究方向图) | 3-5 天 | |
| 7 | Docker 部署 | 2-3 天 | |
| 8 | 测试覆盖率提升 | 持续 | |

> **注意**：Vue 前端迁移不在本项目范围内，由独立前端项目负责。后端保持 WebSocket 协议兼容，同时支持现有 iOS 客户端和未来 Vue 前端。
