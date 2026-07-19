# Paper Agent v4.2 — API 参考文档

> 更新: 2026-07-19 | 版本: 4.2.0 | LLM: DeepSeek v4 Pro + Flash
>
> v4.2 新增: SSE 实时事件流 + 双向控制协议 + 结构化生命周期日志
> v4.1 变动: Agent 需手动启动 / status 响应新增 state/node 字段
> v4.0 新增: Agent 生命周期 / Document CRUD / Preference / Share API
>
> 详见 [vue 客户端 API 参考](../../paper-agent-vue/docs/api-reference.md)

---

## 目录

- [1. 认证 (JWT)](#1-认证-jwt)
- [2. 认证端点](#2-认证端点)
- [3. Agent 管理 (v4.2)](#3-agent-管理-v42)
  - [3.1 REST 端点](#31-rest-端点)
  - [3.2 SSE 事件流端点](#32-sse-事件流端点)
  - [3.3 SSE 事件协议](#33-sse-事件协议)
- [4. 搜索与论文](#4-搜索与论文)
- [5. 知识库](#5-知识库)
- [6. 项目管理](#6-项目管理)
- [7. 文档管理 (v4.0)](#7-文档管理-v40)
- [8. 用户偏好 (v4.0)](#8-用户偏好-v40)
- [9. 会话与消息](#9-会话与消息)
- [10. 订阅](#10-订阅)
- [11. 知识共享 (v4.0)](#11-知识共享-v40)
- [12. 设备注册 (APNs)](#12-设备注册-apns)
- [13. RAG 健康检查](#13-rag-健康检查)
- [14. 调试模式](#14-调试模式)

---

## 1. 认证 (JWT)

v3.1 新增 JWT 认证，替代旧的 Bearer Token API Key 模式。

### 认证方式

所有 API 端点 (除 `/health`、`/sources`、`/auth/*`) 需要在 `Authorization` 头携带 JWT access_token：

```http
Authorization: Bearer <access_token>
```

### 配置

```bash
# .env (必需)
JWT_SECRET=your-secret-key-at-least-32-chars
JWT_ALGORITHM=HS256                          # 默认
ACCESS_TOKEN_EXPIRE_MINUTES=30               # 默认
REFRESH_TOKEN_EXPIRE_DAYS=7                  # 默认
```

### 兼容模式

若 `JWT_SECRET` 未配置，回退到 Bearer Token API Key 模式：
- `API_KEY` 未设置 → 开放访问 (user_id = `"anonymous"`)
- `API_KEY` + token == API_KEY → user_id = `"user-default"`
- `API_KEY` + DB users 表 → 按 `api_token` 查找

---

## 2. 认证端点

Base URL: `http://{host}/api`

### `POST /api/auth/register`

注册新用户 → 返回 JWT tokens。

**Request**:
```json
{
  "username": "alice",
  "password": "secure-password",
  "display_name": "Alice Wang"
}
```

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `username` | string | 3-50 字符 | 登录名，唯一 |
| `password` | string | ≥6 字符 | 密码 |
| `display_name` | string | ≤100 字符 | 显示名称 (可选) |

**Response** (200):
```json
{
  "user_id": "user-a1b2c3d4e5f6",
  "username": "alice",
  "display_name": "Alice Wang",
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer"
}
```

> **v4.1 变更**: 注册后需调用 `POST /api/agents/me/start` 手动启动 Agent。轮询 `GET /api/agents/me/status` 等待 `state: idle`。

**Errors**: 400 (参数不合法) | 409 (用户名已存在)

---

### `POST /api/auth/login`

用户登录 → 返回 JWT tokens。

**Request**:
```json
{
  "username": "alice",
  "password": "secure-password"
}
```

**Response** (200): 同 register

**Errors**: 401 (用户名或密码错误) | 403 (账号已停用)

---

### `POST /api/auth/refresh`

使用 refresh_token 获取新的 access_token。

**Request**:
```json
{
  "refresh_token": "eyJ..."
}
```

**Response** (200): 新的 access_token + refresh_token

**Errors**: 401 (token 无效或过期)

---

### `GET /api/auth/me`

获取当前用户信息 (需要 Bearer Token)。

**Response** (200):
```json
{
  "user_id": "user-a1b2c3d4e5f6",
  "username": "alice",
  "display_name": "Alice Wang",
  "role": "researcher"
}
```

---

## 3. Agent 管理 (v4.2)

v4.2 新增 SSE 事件流端点，替代旧的轮询方式。客户端通过 SSE 实时接收 Agent 状态变更和启动进度，无需设置超时。

### 3.1 REST 端点

#### `GET /api/agents/me`

获取当前用户 Agent 完整信息（配置 + 运行时状态）。

```json
{
  "agent_id": "agent-user-xxx",
  "user_id": "user-xxx",
  "agent_type": "main",
  "display_name": "我的科研助理",
  "system_prompt": "",
  "llm_provider": "deepseek",
  "llm_model": "deepseek-v4-pro",
  "state": "idle",
  "pid": 12345,
  "current_node": "",
  "active_turns": 0,
  "started_at": "2026-07-19T10:00:00Z",
  "last_active_at": "2026-07-19T10:30:00Z",
  "created_at": "2026-07-19T09:00:00Z"
}
```

| 字段 | 说明 |
|------|------|
| `state` | `pending` \| `starting` \| `idle` \| `busy` \| `stopping` \| `stopped` \| `crashed` \| `stalled` |
| `pid` | OS 进程 ID（stopped/crashed/pending 时为 0） |
| `active_turns` | 当前活跃的对话轮数 |
| `current_node` | LangGraph 当前所在节点 |

#### `PUT /api/agents/me`

更新系统提示词。`{"system_prompt": "...", "llm_provider": "deepseek"}`。同步写入 Redis + DB。

#### `GET /api/agents/me/status`

快速查询运行时状态（轻量，仅返回运行时字段）。

```json
{"state": "idle", "pid": 12345, "active_turns": 0, "current_node": "", "last_active_at": "..."}
```

#### `POST /api/agents/me/start` (兼容保留)

同步启动 Agent，超时 180s。返回启动结果。**新客户端建议使用 SSE 端点**。

#### `POST /api/agents/me/stop` (兼容保留)

同步停止 Agent，超时 30s。返回停止结果。**新客户端建议使用 SSE 端点**。

---

### 3.2 SSE 事件流端点

SSE 端点通过长连接实时推送 Agent 生命周期事件。**无超时限制**，连接保持直到操作完成或客户端断开。

#### `GET /api/agents/me/start/stream`

启动 Agent 并流式返回进度。

```
GET /api/agents/me/start/stream
Authorization: Bearer <token>
Accept: text/event-stream
```

响应示例（`text/event-stream`）：

```
event: state
data: {"from":"pending","to":"starting","agent_id":"agent-xxx","timestamp":"..."}

event: progress
data: {"stage":"launch","message":"正在启动 Agent 子进程...","timestamp":"..."}

event: progress
data: {"stage":"bootstrap","message":"Worker 初始化中 (DB+LLM+Tools+Graph)...","elapsed_ms":1200,"timestamp":"..."}

event: state
data: {"from":"starting","to":"idle","agent_id":"agent-xxx","pid":12345,"timestamp":"..."}

event: done
data: {"status":"started","agent_state":{...},"timestamp":"..."}
```

#### `GET /api/agents/me/stop/stream`

停止 Agent 并流式返回进度。

```
event: state
data: {"from":"idle","to":"stopping","agent_id":"agent-xxx"}

event: progress
data: {"stage":"stopping","message":"正在发送 SIGTERM..."}

event: state
data: {"from":"stopping","to":"stopped","agent_id":"agent-xxx"}

event: done
data: {"status":"stopped","uptime_seconds":3600}
```

#### `GET /api/agents/me/restart/stream`

重启 Agent（等价于 stop + start）。

```
event: state
data: {"from":"idle","to":"stopping"}

event: state
data: {"from":"stopping","to":"stopped"}

event: done
data: {"status":"stopped","phase":"stop_complete"}

event: state
data: {"from":"stopped","to":"starting"}

event: progress
data: {"stage":"launch","message":"正在启动 Agent 子进程..."}

event: state
data: {"from":"starting","to":"idle","pid":12346}

event: done
data: {"status":"restarted","agent_state":{...}}
```

---

### 3.3 SSE 事件协议

所有 SSE 事件遵循统一格式：

| 事件类型 | `event` 字段 | `data` 字段 | 说明 |
|----------|-------------|-------------|------|
| 状态转移 | `state` | `{from, to, agent_id, pid?, node?}` | Agent 状态机变更 |
| 进度更新 | `progress` | `{stage, message, elapsed_ms?}` | 操作进度 |
| 操作成功 | `done` | `{status, agent_state?, ...}` | 操作完成，连接关闭 |
| 操作失败 | `error` | `{error, agent_state?}` | 操作失败，连接关闭 |

**`state` 事件** — 每次状态转移触发一次：

```json
{
  "event": "state",
  "data": {
    "from": "pending",
    "to": "starting",
    "agent_id": "agent-user-xxx",
    "pid": 0,
    "node": null,
    "timestamp": "2026-07-19T10:00:00Z"
  }
}
```

**`progress` 事件** — 操作中的阶段性进度：

```json
{
  "event": "progress",
  "data": {
    "stage": "bootstrap",
    "message": "Worker 初始化中...",
    "elapsed_ms": 1200,
    "timestamp": "2026-07-19T10:00:01Z"
  }
}
```

`stage` 取值：`launch` | `bootstrap` | `ready_wait` | `stopping` | `stopped`

**`done` 事件** — 操作完成时的最终事件，发送后 SSE 连接关闭：

```json
{
  "event": "done",
  "data": {
    "status": "started",
    "agent_state": { /* 完整 AgentState */ },
    "elapsed_ms": 3200,
    "timestamp": "2026-07-19T10:00:03Z"
  }
}
```

`status` 取值：`started` | `stopped` | `restarted` | `already_running` | `already_stopped`

**`error` 事件** — 操作失败时的最终事件，发送后 SSE 连接关闭：

```json
{
  "event": "error",
  "data": {
    "error": "Failed to spawn agent subprocess",
    "agent_state": { /* 当前状态 */ },
    "timestamp": "2026-07-19T10:00:02Z"
  }
}
```

#### 客户端使用示例

```javascript
// 原生 EventSource
const es = new EventSource('/api/agents/me/start/stream', {
  // Note: EventSource doesn't support custom headers
  // Use fetch + ReadableStream or pass token via query param
});

es.addEventListener('state', (e) => {
  const { from, to } = JSON.parse(e.data);
  console.log(`State: ${from} → ${to}`);
});

es.addEventListener('progress', (e) => {
  const { message, elapsed_ms } = JSON.parse(e.data);
  console.log(`Progress: ${message} (${elapsed_ms}ms)`);
});

es.addEventListener('done', (e) => {
  const { status, agent_state } = JSON.parse(e.data);
  console.log(`Done: ${status}`);
  es.close();
});

es.addEventListener('error', (e) => {
  // Either a network error or server-sent error event
  console.error('Error:', e.data || 'Network error');
  es.close();
});
```

```javascript
// fetch + ReadableStream (支持 Authorization header)
async function startAgent(token) {
  const response = await fetch('/api/agents/me/start/stream', {
    headers: { 'Authorization': `Bearer ${token}`, 'Accept': 'text/event-stream' }
  });
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += value;
    // Parse SSE events from buffer...
  }
}
```

---

## 4. 搜索与论文

### `GET /api/health`

健康检查，无需认证。

```json
{"status": "ok", "version": "3.1.0"}
```

### `GET /api/sources`

列出可用搜索来源。无需认证。

### `POST /api/search`

跨多源搜索学术论文。结果自动入库 + 关联 project。

**Request**:
```json
{
  "keywords": "transformer attention",
  "sources": "arxiv,semantic_scholar",
  "year_from": 2020,
  "max_results": 20,
  "project_id": null
}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `keywords` | string | `""` | 搜索关键词 |
| `sources` | string | `"arxiv,semantic_scholar"` | 逗号分隔的数据源 |
| `year_from` | int? | null | 起始年份 |
| `year_to` | int? | null | 结束年份 |
| `max_results` | int | 20 | 最大结果数 |
| `project_id` | string? | null | 关联项目 (留空自动创建) |

### `GET /api/papers`

列出论文。**Query**: `project_id`, `relevant_only` (bool), `limit` (int, 默认 50)

### `GET /api/papers/{paper_id}`

获取单篇论文详情。

### `POST /api/papers/upload`

上传 PDF (multipart/form-data)，自动转 Markdown + 入库。

### `POST /api/ingest/start`

触发论文入库流水线 (搜索→评估→下载→转换→索引)，**后台异步执行**。

**Request**:
```json
{
  "user_query": "self-supervised learning survey",
  "sources": ["arxiv", "semantic_scholar"],
  "year_from": 2022,
  "max_results": 20
}
```

### `GET /api/ingest/progress/{task_id}`

查询入库进度 (读取 task.jsonl 日志)。

---

## 4. 知识库

### `POST /api/knowledge/ask`

RAG 问答 (pgvector 向量检索 + LLM)。

**Request**: `{"question": "...", "top_k": 5, "use_fulltext": true}`

### `GET /api/knowledge/search`

语义搜索。**Query**: `q` (必填), `top_k` (默认 5), `project_id` (可选)

### `POST /api/knowledge/extract/{paper_id}`

提取论文结构化知识。**Query**: `deep` (bool)

### `GET /api/knowledge/discover`

研究空白/矛盾/趋势发现。

### `GET /api/knowledge/related/{paper_id}`

发现相关论文。**Query**: `top_k` (默认 10)

---

## 5. 项目管理

### `GET /api/projects`

列出项目。**Query**: `limit` (int, 默认 20)

### `GET /api/projects/{project_id}`

项目详情。

### `DELETE /api/projects/{project_id}`

删除项目。**Query**: `keep_pdfs` (bool, 默认 true)

### `GET /api/projects/{project_id}/export`

导出项目论文。**Query**: `format` (string, 默认 `"bibtex"`)

---

## 7. 文档管理 (v4.0)

`GET/POST /api/documents` — 列出/创建（mode=create/upload/from_paper）
`GET/PUT/DELETE /api/documents/{id}` — CRUD + 乐观锁
`GET /api/documents/{id}/download` — 下载 MD
`GET/POST /api/documents/{id}/versions` — 版本列表/提交
`GET /api/documents/{id}/versions/{vid}` — 版本内容
`POST /api/documents/{id}/revert/{vid}` — 回滚

---

## 8. 用户偏好 (v4.0)

`GET/PUT /api/preferences/me` — 研究领域/写作风格/语言/导师语录

---

## 9. 会话与消息

### `GET /api/sessions/{session_id}/messages`

获取会话的离线消息（替代已移除的 WebSocket sync 协议）。返回去重后的最终状态消息列表。

**Query Parameters**:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `since` | string? | null | ISO 8601 时间戳，只返回此时间之后的消息 |
| `limit` | int | 200 | 最大返回条数 (≤1000) |

**Response** (200):
```json
{
  "messages": [
    {
      "type": "message",
      "subType": "reply",
      "msg_id": "msg-abc123",
      "payload": {"text": "以下是搜索结果..."},
      "timestamp": "2026-07-14T08:00:00Z"
    },
    {
      "type": "tool",
      "subType": "result",
      "msg_id": "tool-def456",
      "payload": {"tool_call_id": "tc-1", "result": "..."},
      "timestamp": "2026-07-14T07:59:55Z"
    }
  ],
  "has_more": false
}
```

**去重规则**:
- `tool/progress|result|start` → 按 `tool_call_id` 去重，只保留最新状态
- `plan_todo_update` → 按 `plan_id` 去重，只保留最新快照
- `message/reply` → 每条保留（不做去重）
- `status` / `thinking` → 不返回（`priority=silent`，不持久化）

**认证**: Bearer Token 必需

---

## 10. 订阅

### `GET /api/subscriptions`

列出所有订阅。

### `POST /api/subscriptions`

创建前沿追踪订阅。

**Request**:
```json
{
  "name": "SSL 前沿",
  "keywords": "self-supervised learning",
  "sources": ["arxiv", "semantic_scholar"],
  "interval_hours": 24
}
```

### `DELETE /api/subscriptions/{subscription_id}`

删除订阅。

### `GET /api/subscriptions/{subscription_id}/results`

推送历史。

### `POST /api/subscriptions/{subscription_id}/check`

手动触发检查。

---

## 11. 知识共享 (v4.0)

`POST /api/share` — 发起共享请求
`GET /api/share/requests` — 请求列表
`PUT /api/share/requests/{id}` — 接受/拒绝

---

## 12. 设备注册 (APNs)

### `POST /api/devices/register`

注册 iOS 设备推送 token。

**Request**:
```json
{
  "agent_id": "agent-alice",
  "device_token": "<hex APNs token>",
  "platform": "ios",
  "bundle_id": "com.example.PaperAgent"
}
```

### `GET /api/devices/{agent_id}`

查看活跃设备 (token 脱敏)。

## 9. RAG 健康检查

### `GET /api/knowledge/health`

查询 RAG 检索系统的运行状态（需要 Bearer Token）。

**Query Parameters**:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `hours` | int | 24 | 统计最近 N 小时的查询 |

**Response** (200):
```json
{
  "total_queries": 142,
  "error_rate": 0.014,
  "latency_p50_ms": 320,
  "latency_p95_ms": 890,
  "status": "healthy"
}
```

数据来源: `rag_traces` 表，每次 RAG 检索 fire-and-forget 写入。

---

## 10. 调试模式

设置 `DEBUG_PROTOCOL=1` 后，服务端通过 WebSocket 推送 `status{level:debug}` 消息：

| stage | 含义 |
|-------|------|
| `llm:thinking` | LLM 完整 thinking block |
| `llm:thinking_delta` | LLM 流式思考 token |
| `llm:tool_use` | LLM 调用 tool |
| `rag:trace` | RAG 检索链路 (retrieval_ms / rerank_ms / total_ms / confidence) |

调试消息 `priority=silent`，不持久化。生产环境不推送。

---

## 快速启动

```bash
# 启动 API Server
uvicorn paper_search.api.app:app --host 0.0.0.0 --port 8000

# 交互式 API 文档
open http://localhost:8000/docs

# 快速测试
curl http://localhost:8000/api/health
```
