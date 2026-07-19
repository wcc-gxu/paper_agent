# CLAUDE.md — Paper Agent v4

> AGENTS.md 含开发命令和环境配置，本文件为详细架构参考。

## 项目概述

Paper Agent v4.1 — 个人 AI 科研助理。Python 后端 (FastAPI + WebSocket) + Vue 3 Web 客户端 + iOS 客户端。

- **编排层**: Agent Supervisor (daemon 容器，管理 N 个 Agent 子进程。stdin/stdout pipe 通信)
- **执行层**: Celery Worker (react_execute, max 8 rounds) + Agent 内部 asyncio 工具
- **Agent 子进程**: 每用户独立 PID，不直接连 Redis。通过 Supervisor 中转消息
- **消息**: Outbox 模式 (Redis List + PostgreSQL + APNs)
- **状态**: Redis Hash `agent:status`（Supervisor 维护，API 查询）
- **控制**: Pub/Sub `agent:control`（仅启停命令）
- **v4.1 新增**: Agent 子进程模型/3 层健康检测/工具二分(in-process vs Celery)/幂等重试
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

| # | 服务 | 端口 | 说明 |
|---|---|---|---|
| 1 | Redis | 6379 | 消息队列 + 状态缓存 |
| 2 | Celery Worker | — | 重型任务（搜索/下载/转换/综述） |
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

- [agent-architecture-v4.md](docs/development/agent-architecture-v4.md) — v4.1 Supervisor + Agent 子进程架构
- [main-agent.md](docs/development/main-agent.md) — MainAgent LangGraph StateGraph 详解
- [anti-hallucination.md](docs/development/anti-hallucination.md) — 反幻觉三层策略
- [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议 v10.2
- [memory-system.md](docs/development/memory-system.md) — 记忆系统架构
- [api-reference.md](docs/development/api-reference.md) — API 参考文档
- [database-architecture.md](docs/development/database-architecture.md) — PostgreSQL schema
- [gap-analysis.md](docs/development/gap-analysis.md) — 当前差距分析
