# CLAUDE.md — Paper Agent v5

> AGENTS.md 含开发命令和环境配置，本文件为详细架构参考。

## 项目概述

Paper Agent v5 — 个人 AI 科研助理。Python 后端 (FastAPI + WebSocket) + Vue 3 Web 客户端 + iOS 客户端。

- **编排层**: Agent Supervisor (daemon 容器，管理 N 个 Agent 子进程。stdin/stdout pipe 通信)
- **执行层**: Celery Worker (异步下载/转换/入库/视频处理) + Agent 内部 asyncio 工具
- **Agent 子进程**: 每用户独立 PID，不直接连 Redis。通过 Supervisor 中转消息
- **消息**: Outbox 模式 (Redis List + PostgreSQL + APNs)
- **状态**: Redis Hash `agent:status`（Supervisor 维护，API 查询）
- **控制**: Pub/Sub `agent:control`（仅启停命令）
- **记忆**: LangGraph 三件套 + pgvector
- **存储**: PostgreSQL+pgvector + Redis + 文件系统
- **定时**: Celery Beat (订阅检查 + health_check + cleanup + 长期抽取 + session_close)

## 主 Agent 架构 (v5)

```
WS 消息 → BRPOP → Safety Filter → MainGraph.ainvoke()
                  ↓                        ↓
          regex + LLM 二次确认      路由式 StateGraph
                                          ↓
                    fast_triage → chat → inline_reply → END
                         ↓
                    intent_classify → ops → ops_confirm → execute(ReAct) → END
                         ↓
                    side_handler（记忆写入: preference / feedback / mentor_quote）
                         ↓
                    [primary 路由 → handler 节点序列 → END]
                         ├─ survey → literature_search_handler
                         ├─ rag → rag_handler
                         ├─ translation → translate_handler
                         ├─ paper_analysis → [前置链?] → paper_handler
                         ├─ survey_generate → [前置链?] → survey_handler
                         ├─ writing → [前置链?] → writing_handler
                         ├─ clustering → [前置链?] → cluster_handler
                         ├─ download → [入库链?] → convert → ingest
                         ├─ citation_chase → citation_handler
                         ├─ glossary → glossary_handler
                         ├─ subscription → subscription_handler
                         ├─ video → video_handler
                         ├─ ingest → ingest_handler
                         └─ memory → memory_handler
```

**v5 核心理念**：
- **Intent = Scenario**：意图即场景，不维护独立的场景 ID 表。14 个业务意图，1:1 映射到 handler 节点序列
- 意图驱动的**确定性路由**，去 LLM 规划（删除 plan/clarify/gate/todo_checkpoint/evaluate 节点）
- 每个 handler 节点自治（内部调工具 + push 消息 + 错误处理）
- 主/辅意图分离（primary 业务意图 + side 记忆写入意图）
- **前置条件链**：5 个意图（paper_analysis/survey_generate/writing/clustering/download）在论文未入库时自动链入 download→convert→ingest
- 用户交互统一走 `ask` 消息；耗时操作进 Celery 前 `ask` 确认

**模型策略**: 结构化判断 (flash + tool_choice) / 生成类 (pro + thinking)

**安全**: 入口 regex → LLM 二次确认 → tool 调用前 regex。fail-closed 纪律。

**错误处理层级**: retry_once → fallback/degradation → LLM advise → ask_user

## Intent 分类 → Handler 路由

`intent_classify` 输出 `{primary, side[], params, route}`：

### Primary Intent 路由表（14 业务意图）

| primary intent | handler 节点序列 | 前置链 | 状态 |
|---------------|-----------------|:---:|:---:|
| `survey` | `literature_search_handler` → END | — | ✅ |
| `rag` | `rag_handler` → END | — | ✅ |
| `ingest` | `ingest_handler` → END | — | ✅ |
| `translation` | `translate_handler` → END | — | 🔧 |
| `writing` | `writing_handler` → END | 论文未入库时自动链 `download→convert→ingest` | 🔧 |
| `glossary` | `glossary_handler` → END | — | 🔧 |
| `clustering` | `cluster_handler` → END | 论文未入库时自动链 `download→convert→ingest` | 🔧 |
| `citation_chase` | `citation_handler` → END | — | 🔧 |
| `paper_analysis` | `paper_handler` → END | 论文未入库时自动链 `download→convert→ingest` | 🔧 |
| `survey_generate` | `survey_handler` → END | 论文未入库时自动链 `download→convert→ingest` | 🔧 |
| `download` | `download_handler` → `convert_handler?` → `ingest_handler?` | 用户要求入库时自动链 | 🔧 |
| `subscription` | `subscription_handler` → END | —（Beat 定时推送为后台进程） | 🔧 |
| `video` | `video_handler` → END | —（handler 内多 tool 串行） | 🔧 |
| `memory` | `memory_handler` → END | — | 🔧 |

> ✅ = 已实现 | 🔧 = 待实现

### Side Intent（在 primary 前处理）

| side intent | 处理节点 | 工具 |
|------------|---------|------|
| `preference` | `side_handler` | `update_preference` |
| `feedback` | `side_handler` | `record_feedback` |
| `mentor_quote` | `side_handler` | `record_feedback` |

### 技术 Intent（不列入业务场景）

| intent | 节点 | 说明 |
|--------|------|------|
| `chat` | `inline_reply` | 非业务闲聊兜底 | ✅ |
| `ops` | `ops_confirm` → `execute(ReAct)` | 运维操作（含磁盘清理） | ✅ |

## Handler 节点列表

| 节点 | 状态 | 工具调用 | 前置链 | Turn 结束? |
|------|:---:|---------|:---:|:---:|
| `fast_triage` | ✅ | 1 次 LLM | — | — |
| `intent_classify` | ✅ | 1 次 LLM | — | — |
| `side_handler` | 🔧 | ReAct ≤ 3 轮 (record_feedback, update_preference) | — | — |
| `rag_handler` | ✅ | `search_kb` → LLM 回答 | — | ✓ |
| `literature_search_handler` | ✅ | `search_papers` → `evaluate_papers` + 保存调研报告 | — | ✓ |
| `ingest_handler` | ✅ | 扫描目录 → `chunk_embed_ingest` (Celery) | — | ✓ |
| `translate_handler` | 🔧 | `glossary_search` → LLM 翻译 | — | ✓ |
| `glossary_handler` | 🔧 | `collect_terms` → `verify_terms` | — | ✓ |
| `citation_handler` | 🔧 | `fetch_citations` → `filter_relevance` | — | ✓ |
| `subscription_handler` | 🔧 | `create_subscription` / `list_subscriptions` | — | ✓ |
| `video_handler` | 🔧 | `download_video`→`transcribe_video`→`summarize_video`→`save_capture` (Celery) | — | ✓ |
| `memory_handler` | 🔧 | `search_memory` / `get_user_preference` | — | ✓ |
| `download_handler` | 🔧 | `download_paper`(s) (N≥10→Celery) | 入库时链 `convert`→`ingest` | ✓ |
| `convert_handler` | 🔧 | `convert_to_md` (Celery) | — | ✓ |
| `paper_handler` | 🔧 | `search_kb(get_fulltext)` → LLM extract | 论文未入库时链 `download→convert→ingest` | ✓ |
| `survey_handler` | 🔧 | LLM generate survey | 论文未入库时链 `download→convert→ingest` | ✓ |
| `writing_handler` | 🔧 | `generate_survey`/`check_ai_flavor`/`gap_analysis` | 论文未入库时链 `download→convert→ingest` | ✓ |
| `cluster_handler` | 🔧 | `cluster_papers` → LLM label | 论文未入库时链 `download→convert→ingest` | ✓ |
| `inline_reply` | ✅ | 纯 LLM | — | ✓ |
| `ops_confirm` | ✅ | 危险确认 | — | — |
| `execute` | ✅ | 自由 ReAct（仅 ops 路径，含 cleanup 磁盘清理） | — | ✓ |

## LLM JSON Schema

| Schema | 用途 | 节点 |
|--------|------|------|
| `SafetyResult` | safe + risk_kind + user_message | C1 安全前置 |
| `IntentClassifyV5Result` | primary + side[] + params + route | intent_classify |

## 出站消息链路 — Outbox 模式

```
MainAgent → outbox_publish() ─┬─ PostgreSQL ws_messages (持久化)
                               └─ Redis LPUSH outbox:{agent_id}
                                        ↓
                              outbox_poller (API 进程)
                              BRPOP → WS (在线) / APNs (离线)
```

| priority | 在线 | 离线 |
|---|---|---|
| silent | WS | 丢弃 |
| normal | WS + 持久化 | 持久化 |
| high | WS + 持久化 | 持久化 + APNs |
| urgent | WS + 持久化 + APNs | 持久化 + APNs |

## 记忆系统 — LangGraph 三件套 + pgvector

| 件 | 作用域 | 实现 |
|---|---|---|
| Checkpointer | thread-scoped | `AsyncPostgresSaver` (graph state) |
| Store | cross-thread | `AsyncPostgresStore` + pgvector (8 namespace) |
| 消息窗口 | 上下文控制 | `trim_messages` + `SummarizationNode` + langmem |

**Store 8 namespace**: preferences / profile / episodes / topics / strategies / errors / knowledge.papers / knowledge.chunks

**三档压缩**: 档1 trim (无LLM, 8k/10条) → 档2 滚动摘要 (同步, hot path) → 档3 长期抽取 (后台 Beat 03:00 + session close)

跨进程 resume: `thread_id = session_id` → `graph.aget_state()` 自动续上

## Tool 体系

所有工具在 `ToolRegistry` 中注册，被 handler 节点调用。无 `agent_` 前缀子 Agent。

| Tool | Handler 节点 | 说明 |
|------|-------------|------|
| `search_papers` | literature_search | BM25+向量混合搜索，跨源（arxiv/S2） |
| `evaluate_papers` | literature_search | LLM 评估论文相关性 |
| `download_paper` | download | 单篇 PDF 下载 |
| `convert_to_md` | convert / ingest | PDF→MD 转换 |
| `search_kb` | rag / paper | BM25+向量检索知识库 |
| `chunk_embed_ingest` | ingest | 切片+embedding+去重+入库 |
| `generate_survey` | survey / writing | LLM 生成文献综述 |
| `check_ai_flavor` | writing | LLM 检测 AI 写作痕迹 |
| `gap_analysis` | writing | LLM 分析研究空白 |
| `glossary_search` | translate | 词表检索匹配 |
| `collect_terms` | glossary | TF-IDF 提取候选术语 |
| `verify_terms` | glossary | LLM 校验术语定义 |
| `cluster_papers` | cluster | K-means 聚类 |
| `fetch_citations` | citation | S2 API 获取引用 |
| `filter_relevance` | citation | LLM 过滤引用相关性 |
| `download_video` | video | yt-dlp 下载视频（Celery） |
| `transcribe_video` | video | Whisper 转写（Celery） |
| `summarize_video` | video | LLM 结构化摘要 |
| `save_capture` | video | 保存碎片知识到 captures 表 |
| `create_subscription` | subscription | 创建订阅 |
| `list_subscriptions` | subscription | 查看订阅列表 / 推送历史 |
| `search_memory` | memory | 语义搜索记忆 |
| `get_user_preference` | memory | 精确查询偏好 |
| `record_feedback` | side | 记录反馈/偏好/语录 |
| `update_preference` | side | 更新用户偏好 |

## 服务进程

| # | 服务 | 端口 | 说明 |
|---|---|---|---|
| 1 | Redis | 6379 | 消息队列 + 状态缓存 |
| 2 | Celery Worker | — | 异步任务（下载/转换/入库） |
| 3 | Celery Beat | — | 定时任务（订阅/健康检查） |
| 4 | API Server | 8000 | FastAPI + WebSocket |
| 5 | Agent Supervisor | — | 管理 N 个 Agent 子进程 + 消息路由 + 状态监控 |

### Agent 子进程隔离

```python
# Supervisor 创建 Agent 子进程
proc = await asyncio.create_subprocess_exec(
    sys.executable, "-m", "paper_search.agent.agent_worker",
    "--user-id", uid,
    stdin=PIPE, stdout=PIPE, stderr=PIPE,
)
# 用户间 OS 级隔离：独立 PID、独立内存空间、崩溃互不影响
# Agent 不直接连 Redis，所有 IO 通过 Supervisor stdin/stdout pipe 中转
```

## PostgreSQL 关键表 (v4.1, 17 张)

| 表 | 用途 |
|---|---|
| `users` | 账户 |
| `agents` | Agent 身份 + 状态 |
| `projects` / `papers` / `project_papers` | 论文项目 |
| `paper_chunks` | 论文切片向量 (pgvector) |
| `captures` | 碎片知识 (含 embedding + 统一 RAG) |
| `glossary_terms` | 术语库 (含 embedding) |
| `journal_ranks` | CCF/SCI 分级缓存 |
| `sessions` / `ws_messages` | 会话 + 消息持久化 |
| `documents` | 文档 CRUD (含版本历史 JSONB) |
| `user_preferences` | 用户偏好 |
| `subscriptions` | 订阅 (含结果 JSONB) |
| `share_requests` | 细粒度共享 |
| `hallucination_events` | 反幻觉审计 |
| `event_logs` | 通用事件日志 |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | LangGraph Checkpointer (自动管理) |
| `store_data` | LangGraph Store (自动管理) |


## Redis Key 清单

| Key | 类型 | 写 | 读 | 作用 |
|-----|------|:---:|:---:|---|
| `agent:status` | Hash | Supervisor | API | 全量 Agent 状态（state/node/pid/active_turns） |
| `agent:ws:{uid}` | List | API | Supervisor | 入站消息队列 |
| `agent:outbox:{uid}` | List | Supervisor | API | 出站消息队列 |
| `agent:control` | Pub/Sub | API | Supervisor | 控制指令（仅启停） |
| `agent:ws:{uid}:parked` | List | Supervisor | Supervisor | 未匹配消息暂存 |
| `outbox:{uid}` | List | Agent outbox | outbox_poller | 出站消息（兼容旧版） |
| `agent:reports:{uid}` | Pub/Sub | 子 Agent | Agent | 子 Agent 上报（保留，中继使用） |
| `agent:notifications` | Pub/Sub | Celery Beat | API | 订阅通知 |
| `session:{sid}` | Hash | handler 节点 | handler 节点 | 跨 turn 状态（v5 新增） |

## Environment (.env)

| Variable | Purpose |
|---|---|
| `VOLCANO_API_KEY` | LLM (火山方舟) |
| `LLM_BASE_URL` | 默认 `https://api.deepseek.com/anthropic` |
| `LLM_MODEL` | 默认 `deepseek-v4-pro` |
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar |
| `ELSEVIER_API_KEY` / `IEEE_API_KEY` | ScienceDirect / IEEE |
| `WEB_SEARCH_API_KEY` | 火山引擎联网搜索 |
| `DATABASE_URL` | PostgreSQL (必需) |
| `REDIS_URL` | 默认 `redis://localhost:6379/0` |
| `EMBEDDING_API_KEY` | Embedding (Doubao) |
| `RERANK_API_KEY` | SiliconFlow Cross-Encoder |
| `RERANK_MODEL` | 默认 `BAAI/bge-reranker-v2-m3` |
| `INTENT_ASK_THRESHOLD` | C3 灰区阈值 (默认 `0.6`) |
| `DEBUG_PROTOCOL` | `=1` 推送 debug 消息 |

## Dependencies

```
langgraph>=0.6, langgraph-checkpoint-postgres>=2.0, langmem>=0.0.10
fastapi>=0.110, uvicorn[standard]>=0.27
celery[redis]>=5.4, redis>=5.0
pgvector>=0.3, psycopg2-binary>=2.9
pymupdf4llm, arxiv>=2.3, biopython, metapub
rich>=13, python-dotenv
```

Python >= 3.11

## 文档索引

- [intent-routing-design.md](docs/development/intent-routing-design.md) — v5 意图-场景-节点路由设计
- [architecture-upgrade-v5.md](docs/development/architecture-upgrade-v5.md) — v5 架构升级方案
- [v5-development-plan.md](docs/development/v5-development-plan.md) — v5 分阶段开发计划
- [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议 v11.1
- [memory-system.md](docs/development/memory-system.md) — 记忆系统架构
- [api-reference.md](docs/development/api-reference.md) — API 参考文档
- [database-architecture.md](docs/development/database-architecture.md) — PostgreSQL schema
- [anti-hallucination.md](docs/development/anti-hallucination.md) — 反幻觉三层策略
