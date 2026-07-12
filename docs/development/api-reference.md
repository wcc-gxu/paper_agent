# Paper Agent v3 — API 参考文档

> 更新: 2026-07-12 | 版本: 3.1.0 | LLM: DeepSeek v4 Pro + Flash
>
> 架构: Fast Triage (flash) → chat/ops/research → Intent Classify (flash) → Plan (pro) → Execute ReAct (pro) → Evaluate (flash)

---

## 目录

- [1. WebSocket 端点](#1-websocket-端点)
- [2. REST API 端点](#2-rest-api-端点)
- [3. 调试模式](#3-调试模式)
- [3. 认证](#3-认证)
- [4. 协议一致性检查](#4-协议一致性检查)

---

## 1. WebSocket 端点

### 1.1 连接地址

```
ws://{host}:{port}/ws/chat/{agent_id}/{session_id}
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `agent_id` | `agent-001` | Agent 实例 ID。格式 `agent-{user_id}`，v3 Phase 1 从中提取 user_id |
| `session_id` | `main` | 会话 ID。同一 agent 可有多 session 并行 |

**地址来源**: `src/paper_search/api/app.py:157` — `@app.websocket("/ws/chat/{agent_id}/{session_id}")`

**默认值来源**:
- `agent_id` → 文档 §1.1 约定 `"agent-001"`；测试客户端默认 `"agent-001"` (`tests/test_ws_client.py:37`)
- `session_id` → `main_agent.py:635,2593` 代码内回退默认 `"main"`

### 1.2 连接流程

```
iOS 连接 → WS accept → outbox_poller 启动 → Redis LPUSH 入站队列
                                                  ↓
                                         Daemon BRPOP 消费
                                                  ↓
                                         outbox_publish 出站
                                                  ↓
                                         poller BRPOP → WS send_text
```

无握手协议。连接即用。服务端永不主动断开。

### 1.3 消息协议

详见 [websocket-protocol.md](websocket-protocol.md) (v10.0)。

**出站消息类型** (server → client):

| type | subType | priority | 说明 |
|------|---------|----------|------|
| `status` | — | `normal` | 人类可读阶段更新 |
| `message` | `reply` | `high` | LLM 最终 Markdown 回复 |
| `tool` | `start` | `high` | 启动子 Agent / 长任务 |
| `tool` | `progress` | `normal` | 任务进度更新 |
| `tool` | `result` | `high` | 任务终态 (done/failed) |
| `tool` | `call` | `high` | 请求 iOS 执行本地 tool |
| `ask` | — | `high` | 用户交互卡片 (唯一交互入口) |
| `error` | `TASK_FAILED` / `INTERNAL_ERROR` / `ASK_TIMEOUT` | `urgent` | 错误 |
| `pong` | — | `silent` | 心跳回复 |
| `sync_complete` | — | `silent` | 重连回放完毕 |

**入站消息类型** (client → server):

| type | 说明 |
|------|------|
| `ping` | 心跳 |
| `message` | 用户文本输入 |
| `ask_reply` | Ask Card 回执 (统一) |
| `tool_result` | iOS-side tool 执行结果 |
| `sync` | 重连同步请求 |

### 1.4 重连同步

```
[iOS] WS connect → [Server] accept
[iOS] sync {payload: {last_msg_id?: "..."}}
[Server] 逐条回放 ws_messages 中本 session 未送达消息
[Server] sync_complete {payload: {synced_count: N}}
```

---

## 2. REST API 端点

Base URL: `http://{host}:8000/api`

所有端点 (除 `/health` 和 `/sources`) 需要 Bearer Token 认证。

### 2.1 Health & Meta

#### `GET /api/health`

健康检查，无需认证。

**Response**:
```json
{"status": "ok", "version": "3.0.0"}
```

---

#### `GET /api/sources`

列出所有可用搜索来源及其可用性。无需认证。

**Response**:
```json
{
  "total": 6,
  "sources": [
    {"name": "arxiv", "description": "arXiv 预印本", "available": true},
    {"name": "semantic_scholar", "description": "Semantic Scholar", "available": true},
    {"name": "pubmed", "description": "PubMed", "available": false},
    {"name": "cnki", "description": "CNKI 中国知网", "available": false},
    {"name": "ieee", "description": "IEEE Xplore", "available": false},
    {"name": "sciencedirect", "description": "ScienceDirect", "available": false}
  ]
}
```

---

### 2.2 Search

#### `POST /api/search`

跨多源搜索学术论文。结果自动入库 + 关联 project。

**Request**:
```json
{
  "keywords": "transformer attention",
  "sources": "arxiv,semantic_scholar",
  "title": null,
  "author": null,
  "doi": null,
  "year_from": null,
  "year_to": null,
  "max_results": 20,
  "project_id": null
}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `keywords` | string | `""` | 搜索关键词 |
| `sources` | string | `"arxiv,semantic_scholar"` | 逗号分隔的数据源 |
| `title` | string? | null | 按标题搜索 |
| `author` | string? | null | 按作者搜索 |
| `doi` | string? | null | 按 DOI 搜索 |
| `year_from` | int? | null | 起始年份 |
| `year_to` | int? | null | 结束年份 |
| `max_results` | int | 20 | 最大结果数 |
| `project_id` | string? | null | 关联的项目 ID (留空自动创建) |

**Response**:
```json
{
  "success": true,
  "project_id": "proj-...",
  "total_found": 42,
  "sources_searched": ["arxiv", "semantic_scholar"],
  "errors": [],
  "paper_ids": ["sha256:...", ...],
  "papers": [
    {
      "title": "Attention Is All You Need",
      "authors": ["Vaswani A", "Shazeer N", ...],
      "year": 2017,
      "abstract": "The dominant sequence transduction models...",
      "doi": "10.5555/3295222.3295349",
      "arxiv_id": "1706.03762",
      "source": "arxiv",
      "citation_count": 138492,
      "venue": "NeurIPS 2017"
    }
  ]
}
```

---

### 2.3 Papers

#### `GET /api/papers`

列出论文。

**Query Parameters**:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `project_id` | string? | null | 按项目过滤 |
| `relevant_only` | bool | false | 仅返回相关论文 |
| `limit` | int | 50 | 最大返回数 (无 project_id 时) |

**Response**:
```json
{
  "total": 12,
  "papers": [...]
}
```

---

#### `GET /api/papers/{paper_id}`

获取单篇论文详情。

**Response**: 论文完整字段 dict。

**Errors**: 404 — Paper not found

---

#### `POST /api/papers/upload`

上传本地 PDF，自动转 Markdown + 入库。

**Request**: `multipart/form-data`
- `file`: PDF 文件 (仅支持 .pdf)
- `project_id`: (可选) 关联项目 ID

**Response**:
```json
{
  "success": true,
  "paper_id": "sha256:...",
  "title": "my-paper",
  "pdf_path": "/path/to/papers/uploads/my-paper.pdf",
  "markdown_path": "/path/to/markdown/my-paper.md"
}
```

**Errors**: 400 — Only PDF files are supported

---

### 2.4 Knowledge Base

#### `POST /api/knowledge/ask`

知识库 RAG 问答 (ChromaDB 向量检索 + LLM)。

**Request**:
```json
{
  "question": "transformer 的核心贡献是什么？",
  "top_k": 5,
  "use_fulltext": true
}
```

**Response**:
```json
{
  "question": "transformer 的核心贡献是什么？",
  "answer": "Transformer 的核心贡献是...",
  "confidence": 0.92,
  "sources": [{"title": "...", "chunk_id": "...", "score": 0.89}],
  "follow_up_questions": ["..."]
}
```

---

#### `GET /api/knowledge/search`

知识库语义搜索。

**Query Parameters**: `q` (必填), `top_k` (默认 5), `project_id` (可选)

---

#### `POST /api/knowledge/extract/{paper_id}`

提取论文结构化知识 (method/contribution/limitation)。

**Query Parameters**: `deep` (bool, 默认 false — 深度提取)

---

#### `GET /api/knowledge/discover`

知识发现 — 研究空白、矛盾、趋势。

**Query Parameters**: `domain` (string), `project_id` (可选)

---

#### `GET /api/knowledge/related/{paper_id}`

发现相关论文。

**Query Parameters**: `top_k` (int, 默认 10)

---

### 2.5 Agent Tasks

#### `POST /api/tasks`

创建 Agent 任务。

**Query Parameters**: `query` (string, 必填 — 研究需求描述)

**Response**:
```json
{"task_id": "task-20260712-a1b2", "status": "pending", "query": "..."}
```

---

#### `POST /api/tasks/{task_id}/confirm`

确认/拒绝 Plan，触发执行或取消。

**Request**:
```json
{
  "task_id": "task-20260712-a1b2",
  "confirmed": true,
  "modifications": null
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `confirmed` | bool | true=批准执行, false=取消 |
| `modifications` | dict? | 用户修改项 |

---

#### `POST /api/tasks/{task_id}/pause`

暂停任务 (当前阶段完成后暂停)。

#### `POST /api/tasks/{task_id}/resume`

恢复已暂停的任务。

#### `DELETE /api/tasks/{task_id}`

取消任务。

---

### 2.6 Ingest (论文入库)

#### `POST /api/ingest/start`

触发论文入库流水线 (搜索→评估→下载→转换→索引)，**后台异步执行**。

**Request**:
```json
{
  "user_query": "self-supervised learning survey",
  "sources": ["arxiv", "semantic_scholar"],
  "year_from": 2022,
  "max_results": 20,
  "project_id": null
}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `user_query` | string | (必填) | 搜索查询 |
| `sources` | string[] | `["arxiv","semantic_scholar"]` | 数据源列表 |
| `year_from` | int | 2022 | 起始年份 |
| `max_results` | int | 20 | 最大结果数 |
| `project_id` | string? | null | 关联项目 (留空自动创建) |

**Response**:
```json
{
  "task_id": "task-20260712-c3d4",
  "project_id": "proj-...",
  "status": "started",
  "query": "self-supervised learning survey"
}
```

---

#### `GET /api/ingest/progress/{task_id}`

查询入库进度 (读取 task.jsonl 日志)。

**Response**:
```json
{
  "task_id": "task-20260712-c3d4",
  "progress": {"stage": "download", "current": 12, "total": 50},
  "event_count": 35,
  "latest_events": [...]
}
```

---

### 2.7 Projects

#### `GET /api/projects`

列出项目。**Query**: `limit` (int, 默认 20)

#### `GET /api/projects/{project_id}`

项目详情 + 论文数。

#### `DELETE /api/projects/{project_id}`

删除项目。**Query**: `keep_pdfs` (bool, 默认 true — 保留论文仅删关联，false 则连论文一起删)

#### `GET /api/projects/{project_id}/export`

导出项目论文。**Query**: `format` (string, 默认 `"bibtex"`，支持 `"json"`)

---

### 2.8 Subscriptions

#### `GET /api/subscriptions`

列出所有订阅。

#### `POST /api/subscriptions`

创建订阅。

**Request**:
```json
{
  "name": "SSL 前沿",
  "keywords": "self-supervised learning",
  "sources": ["arxiv", "semantic_scholar"],
  "interval_hours": 24
}
```

#### `DELETE /api/subscriptions/{subscription_id}`

删除订阅及所有推送结果。

#### `GET /api/subscriptions/{subscription_id}/results`

订阅推送历史。

**Query Parameters**: `since` (ISO datetime, 可选), `limit` (int, 默认 50, max 200)

#### `POST /api/subscriptions/{subscription_id}/check`

手动触发一次订阅检查 (Celery 异步执行)。

**Response**:
```json
{
  "success": true,
  "celery_task_id": "abc-def-...",
  "subscription_name": "SSL 前沿",
  "message": "Check triggered for 'SSL 前沿'"
}
```

---

### 2.9 Devices / APNs

#### `POST /api/devices/register`

注册 iOS device token (APNs 推送)。

**Request**:
```json
{
  "agent_id": "agent-001",
  "device_token": "<hex APNs token>",
  "platform": "ios",
  "bundle_id": "com.example.PaperAgent"
}
```

**Errors**: 400 — Invalid device_token

---

#### `GET /api/devices/{agent_id}`

查看某 agent 的活跃设备列表 (调试用，token 脱敏)。

**Response**:
```json
{
  "agent_id": "agent-001",
  "count": 1,
  "devices": [
    {
      "platform": "ios",
      "bundle_id": "com.example.PaperAgent",
      "token_prefix": "a1b2c3d4e5f6",
      "created_at": "...",
      "last_seen_at": "..."
    }
  ]
}
```

---

## 3. 调试模式

### 3.1 启用调试

设置环境变量 `DEBUG_PROTOCOL=1` 后重启服务：

```bash
DEBUG_PROTOCOL=1 PYTHONPATH=src .venv/bin/python -m paper_search.agent.daemon
```

### 3.2 调试消息类型

调试模式下，服务端通过 WebSocket 推送 `status{level:debug}` 消息，包含 LLM 内部思考过程：

| stage | 含义 | 触发时机 |
|-------|------|----------|
| `llm:thinking` | LLM 返回完整 thinking block | 非流式 `chat`/`chat_json` 响应包含 `type=thinking` |
| `llm:thinking_delta` | LLM 流式思考 token | `chat_stream` 收到 `thinking_delta` SSE 事件 |
| `llm:tool_use` | LLM 调用 tool | 流式响应中 `content_block_start{type=tool_use}` |

**消息格式**：
```json
{
  "type": "status",
  "priority": "silent",
  "payload": {
    "stage": "llm:thinking_delta",
    "message": "{'thinking': 'We need to analyze...'}",
    "level": "debug"
  }
}
```

**注意事项**：
- 调试消息 `priority=silent`，不持久化、不触发 APNs
- 生产环境（未设 `DEBUG_PROTOCOL`）不推送任何 debug 消息
- iOS 生产 build 不渲染 `level=debug` 的 status

### 3.3 快速诊断命令

```bash
# 查看最近的 LLM 请求
grep "HTTP Request.*v1/messages" /tmp/paper_agent_daemon.log | tail -5

# 查看 debug 消息
grep "llm:thinking" /tmp/paper_agent_daemon.log | tail -10

# 检查 LLM API 连通性
curl -s https://api.deepseek.com/anthropic/v1/messages \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-pro","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
```

---

## 4. 认证

### 认证模式

| 配置 | 模式 | 行为 |
|------|------|------|
| `API_KEY` 未设置 | **开放访问** | 所有请求 user_id = `"anonymous"` |
| `API_KEY` = `"sk-xxx"` | **单用户** | token == API_KEY → user_id = `"user-default"` |
| `API_KEY` + users 表 | **多用户** | 按 `api_token` 查 users 表 → 返回对应 user_id |

### 使用方式

```http
Authorization: Bearer <your-api-key>
```

### 配置

```bash
# .env
API_KEY=sk-your-secret-key
```

### 多用户模式 (v3 Phase 1)

系统支持从 `users` 表按 token 查找用户。默认迁移用户:
- user_id: `"user-default"`, token: `"tok-migrated-default"`

代码定义: `src/paper_search/api/auth.py`

---

## 5. 协议一致性检查

以下对照 [websocket-protocol.md](websocket-protocol.md) (v10.0) 与当前代码实现逐项检查。

### 4.1 WS 地址

| 项目 | 文档 | 代码 | 一致? |
|------|------|------|:---:|
| 路径模板 | `/ws/chat/{agent_id}/{session_id}` | `app.py:157` `@app.websocket("/ws/chat/{agent_id}/{session_id}")` | ✅ |
| 默认 agent_id | `"agent-001"` | 文档 §1.1 + `test_ws_client.py:37` 默认 `"agent-001"` | ✅ |
| 默认 session_id | `"main"` | `main_agent.py:635,2593` 回退默认 `"main"` | ✅ |
| agent_id 格式 | `agent-{user_id}` | `app.py:175-180` 从 `agent-` 前缀提取 user_id，`"001"` → `"default"` | ✅ |
| 连接无握手 | 连接即用 | `app.py:182` `await websocket.accept()` → 直接进入消息循环 | ✅ |
| 永不主动断开 | 永不主动断开 | `app.py:273` `while True:` + WSDisconnect 仅 break 不 close | ✅ |

### 4.2 入站消息处理

| 文档类型 | 代码处理 | 位置 | 一致? |
|----------|----------|------|:---:|
| `ping` | `msg_type == "ping"` → pong 回复 | `app.py:336` | ✅ |
| `message` | `msg_type == "message"` 补 `subType: "chat"` 后 LPUSH | `app.py:357-360` | ✅ |
| `ask_reply` | LPUSH 到 Redis，main_agent `_wait_ws_reply` 匹配 | `app.py:367` + `main_agent.py:2356` | ✅ |
| `tool_result` | LPUSH 到 Redis，main_agent `_wait_ws_reply` 匹配 | 同上 | ✅ |
| `sync` | `msg_type in ("sync", "sync_request")` → `_handle_sync_request` | `app.py:349-351` | ✅ (兼容旧名) |
| `capabilities` | 缓存到 `_capabilities_cache` | `app.py:331-333` | ✅ |

### 4.3 出站消息 Priority 默认值

| (type, subType) | 文档 priority | 代码 PRIORITY_DEFAULTS | 一致? |
|-----------------|:------------:|:----------------------:|:---:|
| `status`, — | **`normal`** | `silent` | ⚠️ **不一致** |
| `message`, `reply` | `high` | `high` | ✅ |
| `tool`, `start` | `high` | `high` | ✅ |
| `tool`, `progress` | `normal` | `normal` | ✅ |
| `tool`, `result` | `high` | `high` | ✅ |
| `tool`, `call` | `high` | `high` | ✅ |
| `ask`, — | `high` | `high` | ✅ |
| `error`, `TASK_FAILED` | `urgent` | `urgent` | ✅ |
| `error`, `INTERNAL_ERROR` | `urgent` | `urgent` | ✅ |
| `error`, `ASK_TIMEOUT` | `urgent` | `urgent` | ✅ |
| `pong`, — | `silent` | `silent` | ✅ |
| `sync_complete`, — | `silent` | `silent` | ✅ |

### 4.4 ⚠️ 不一致详情

#### `status` priority: 文档 `normal` vs 代码 `silent`

| 来源 | 值 | 影响 |
|------|-----|------|
| [websocket-protocol.md §1.5](#15-消息速查全表) | `normal` | status 消息应持久化到 `ws_messages` 表 |
| `outbox.py:39` | `silent` | status 消息 **不持久化**，重连后丢失 |
| `main_agent.py:2353` | `silent` | 注释: "status 不触发 APNs" |

**分析**: 代码选择 `silent` 的理由是 status 是瞬时进度反馈，重连后回放阶段消息没有意义。但文档写 `normal` 可能是为了在 `ws_messages` 表留痕用于调试。**建议**: 统一为 `silent`（更新文档）或改为 `normal` 并去重（更新代码）。

#### `error` subType 新增项

文档 §1.5 只列了 3 种 `error` subType（`TASK_FAILED` / `INTERNAL_ERROR` / `ASK_TIMEOUT`），但代码增加了：

| subType | priority | 用途 |
|---------|----------|------|
| `MAX_ROUNDS` | `high` | 达到最大轮数上限 |
| `PERMISSION_DENIED` | `high` | 权限不足 |

**建议**: 文档补充这两种 error subType。

#### v10 协议迁移过渡态

`PRIORITY_DEFAULTS` (outbox.py:37-65) 同时包含 v10 和 v9 条目。以下 v9 类型为兼容保留：

| v9 (type, subType) | v10 替代 | 状态 |
|-------------------|----------|:---:|
| `message`, `thinking` | (删除) | 兼容保留 |
| `message`, `text` | `message`, `reply` | 兼容保留 |
| `tool`, `sub_request` | `tool`, `start` | 兼容保留 |
| `tool`, `sub_progress` | `tool`, `progress` | 兼容保留 |
| `tool`, `sub_result` | `tool`, `result` | 兼容保留 |
| `tool`, `ask_user_question` | `ask` | 兼容保留 |
| `tool`, `propose_plan` | `ask` (kind=plan) | 兼容保留 |
| `tool`, `ios_request` | `tool`, `call` | 兼容保留 |
| `tool`, `ios_result` | `tool_result` (inbound) | 兼容保留 |

### 4.5 Ask 超时 APNs

文档 §7.1 描述了两阶段超时机制，但代码中尚未实现（`ASK_TIMEOUT_SECONDS` 环境变量未在代码中引用）。当前是 **骨架状态**。实现文件: `outbox_poller.py`。

### 4.6 DEBUG_PROTOCOL

文档 §5.2 和 §9 提到 `DEBUG_PROTOCOL=1` 环境变量控制 `status{level:debug}` 消息，当前代码中 `_push_status` 使用 `level="user"` 或 `level="info"`，未区分 `debug` 级别。**尚未实现**。

---

## 6. 快速启动

```bash
# 启动 API Server
uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000

# 测试健康检查
curl http://localhost:8000/api/health

# 测试 WebSocket
python tests/test_ws_client.py --host ws://localhost:8000
```

### 交互式 API 文档

FastAPI 自动生成 Swagger UI:
```
http://localhost:8000/docs
```
