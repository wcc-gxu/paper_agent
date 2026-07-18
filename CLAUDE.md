# CLAUDE.md — Paper Agent v4

> AGENTS.md 含开发命令和环境配置，本文件为详细架构参考。

## 项目概述

Paper Agent v4.0 — 个人 AI 科研助理。Python 后端 (FastAPI + WebSocket) + Vue 3 Web 客户端 + iOS 客户端。

- **编排层**: Supervision Agent (daemon: intent_classify → plan ⇄ clarify → plan_review)
- **执行层**: Celery Worker (react_execute, max 8 rounds)
- **消息**: Outbox 模式 (Redis List + PostgreSQL + APNs)
- **v4.0 新增**: Agent 生命周期/文档管理/用户偏好/知识库共享/Celery 执行拆分/Redis 心跳
- **记忆**: LangGraph 三件套 + pgvector
- **存储**: PostgreSQL+pgvector + Redis + 文件系统
- **定时**: Celery Beat (订阅检查 + health_check + cleanup + 长期抽取 + session_close)

## 主 Agent 架构

```
WS 消息 → BRPOP → Safety Filter → MainGraph.ainvoke()
                  ↓                        ↓
          regex + LLM 二次确认      10-node StateGraph
                                          ↓
                          fast_triage → intent_classify → plan → gate
                             → execute(ReAct) → todo_checkpoint → evaluate
```

**模型策略**: 结构化判断 (flash + tool_choice) / 生成类 (pro + thinking)

**安全**: 入口 regex → LLM 二次确认 → tool 调用前 regex。fail-closed 纪律。

**evaluate 5 出口**: `done` / `retry_tools` / `ask_user` / `replan` / `fail`

## 17 个业务场景

`intent_classify` 映射到 1~N 个 scenario_id（支持复合意图）：

| ID | 场景 | ID | 场景 |
|---|---|---|---|
| S1 | 文献调研/筛选 | S10 | RAG 问答 |
| S2 | 文献综述生成 | S11 | 批量搜索 |
| S3 | 每日前沿追踪 (订阅) | S12 | 学术翻译/术语库 |
| S4 | 论文精读/提炼 | S13 | 视频解析 |
| S5 | 方法对比 | S14 | 导出/清理 |
| S6 | 研究空白分析 | S15 | iOS 自动化 |
| S7 | 进度查看 | S16 | 运维操作 |
| S8 | 聚类+全景图 | S17 | 记忆操作 |
| S9 | 引用追溯 | | |

## LLM JSON Schema 强约束

| Schema | 用途 | 节点 |
|---|---|---|
| `SafetyResult` | safe + risk_kind + user_message | C1 安全前置 |
| `IntentClassifyResult` | intent_kind + scenarios[] + confidence | C2 意图分类 |
| `ScenarioPlanResult` | summary + needs_clarify + permissions + tools[] | plan |
| `EvaluateCompletionResult` | satisfied + next_action + final_message | evaluate |

## 出站消息链路 — Outbox 模式

```
MainAgent → outbox_publish() ─┬─ PostgreSQL ws_messages (持久化)
                               └─ Redis LPUSH outbox:{agent_id}
                                        ↓
                              outbox_poller (API 进程)
                              BRPOP → WS (在线) / APNs (离线)
```

| priority_kind | 在线 | 离线 |
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

## 8 个子 Agent

| Agent | Graph | 流程 |
|---|---|---|
| Literature | `literature_graph.py` | search → evaluate → download → convert → metadata |
| Knowledge | `knowledge_graph.py` | Ingest(embed→dedup→rank) + Query(parse→route→search→evaluate) |
| Clustering | `clustering_graph.py` | cluster → label → detect |
| CitationChase | `citation_chase_graph.py` | resolve → fetch → filter → ingest → decide → summarize |
| Translation | `translation_graph.py` | route → translate/build_glossary/enrich |
| Glossary | `glossary_graph.py` | collect → search → verify → evolve |
| Video | `video_graph.py` | download → transcribe → summarize |
| Writing | `writing_graph.py` | survey → citation_check → ai_flavor_check |

Sub-agent 工具使用 `agent_` 前缀 (e.g., `agent_literature_search`)。

## 服务进程

| # | 服务 | 端口 |
|---|---|---|
| 1 | Redis | 6379 |
| 2 | Celery Worker | — |
| 3 | Celery Beat | — |
| 4 | API Server | 8000 |
| 5 | Agent Daemon | — |

## PostgreSQL 关键表

| 表 | 用途 |
|---|---|
| `users` / `sessions` | 账户 + 会话 |
| `agents` | 多 Agent 身份 |
| `projects` / `papers` / `project_papers` | 论文项目 |
| `ws_messages` | 出站消息持久化 |
| `message_embeddings` | 消息向量召回 (pgvector) |
| `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` | LangGraph Checkpointer |
| `store_data` | LangGraph Store |
| `paper_chunks` / `glossary_embeddings` / `session_summaries` / `topic_embeddings` | 向量索引 (pgvector) |
| `conversation_archive` | 摘要归档 |
| `hallucination_events` | 反幻觉审计 |
| `rag_traces` | RAG 可观测性 |
| `agent_tasks` / `task_steps` | 任务跟踪 |
| `external_validations` | 引用验证 |
| `subscriptions` / `subscription_results` | 订阅管理 |

## Redis Key 清单

| Key | 类型 | 作用 |
|---|---|---|
| `agent:ws:{agent_id}` | List | iOS → Agent 入站 |
| `agent:ws:{agent_id}:parked` | List | 暂存消息 |
| `outbox:{agent_id}` | List | Agent → iOS 出站 |
| `agent:reports:{agent_id}` | Pub/Sub | 子 Agent 上报 |
| `agent:notifications` | Pub/Sub | Beat → API |
| `vec:cache:{user_id}:{md5(query)}` | String | 向量缓存 (TTL 15min) |

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

- [main-agent.md](docs/development/main-agent.md) — MainAgent LangGraph StateGraph 详解
- [anti-hallucination.md](docs/development/anti-hallucination.md) — 反幻觉三层策略
- [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议 v10.2
- [memory-system.md](docs/development/memory-system.md) — 记忆系统架构
- [api-reference.md](docs/development/api-reference.md) — API 参考文档
- [database-architecture.md](docs/development/database-architecture.md) — PostgreSQL schema
- [agent-architecture-v4.md](docs/development/agent-architecture-v4.md) — v4 目标架构
- [gap-analysis.md](docs/development/gap-analysis.md) — 当前差距分析
