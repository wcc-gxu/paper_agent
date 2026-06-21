# Paper Agent v3 — WebSocket 通信协议

> iOS 客户端对接规范 | v9.1 | 2026-06-21

---

## 一、总纲

### 1.1 WebSocket 地址

```
ws://{host}:{port}/ws/chat/{agent_id}/{session_id}
```

| 参数 | 说明 |
|------|------|
| `agent_id` | Agent 实例 ID。默认 `"agent-001"` |
| `session_id` | 会话 ID。默认 `"main"` |

### 1.2 连接建立

无握手。iOS 连接后立即发 ping，服务端回 pong，开始通信。

```
→ {"type":"ping","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{}}
← {"type":"pong","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{}}
```

服务端永不主动断开连接。

### 1.2.1 重连同步

iOS 重连后**应主动发送** `sync_request` 拉取离线期间错过的消息（详见 §2.6）。

```
[iOS] WS connect → [Server] accept
[iOS] sync_request {last_msg_id?: "..."}
[Server] (逐条回放) ws_messages 中本 session 未送达的消息
[Server] sync_complete {synced_count: N}
```

### 1.3 通用信封

```json
{
  "type": "message | tool | ping | pong | error | sync_request | sync_complete",
  "subType": "<子类>",
  "role": "user | assistant | tool | system",
  "agentId": "agent-001",
  "sessionId": "main",
  "timestamp": "2026-06-20T12:00:00Z",
  "msg_id": "uuid-v4",
  "priorityKind": "silent | normal | high | urgent",
  "payload": {}
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息大类 |
| `subType` | string | 消息子类 |
| `role` | string | `user`=iOS发出, `assistant`=LLM/主Agent发出, `tool`=工具返回, `system`=后台任务状态 |
| `agentId` | string | Agent 实例 ID |
| `sessionId` | string | 会话 ID |
| `timestamp` | string | ISO 8601 UTC |
| `msg_id` | string | **v9.1 新增**。出站消息的 UUID，iOS 用于回传 ack / 排重 / `sync_request.last_msg_id` |
| `priorityKind` | string | **v9.1 新增**。`silent`=不持久化(流式 thinking) / `normal`=普通进度 / `high`=任务完成/提问 → 触发 APNs / `urgent`=错误/订阅推送 |
| `payload` | object | 消息体 |

### 1.4 消息速查全表

| type | subType | role | →/← | 说明 |
|------|---------|------|:---:|------|
| `ping` | — | `user` | → | 心跳 |
| `pong` | — | `assistant` | ← | 心跳回复 |
| `message` | `chat` | `user` | → | 用户聊天消息 |
| `message` | `text` | `assistant` | ← | LLM 完整回复 |
| `message` | `thinking` | `assistant` | ← | LLM 思考过程 (流式, silent) |
| `tool` | `ios_request` | `assistant` | ← | 请求 iOS 执行工具 |
| `tool` | `ios_result` | `tool` | → | iOS 工具返回结果 |
| `tool` | `ask_user_question` | `assistant` | ← | 请求 iOS 向用户提问 |
| `tool` | `ask_user_question` | `user` | → | 用户回答 |
| `tool` | `propose_plan` | `assistant` | ← | **v9.1** 待用户审批的结构化方案 |
| `tool` | `propose_plan` | `user` | → | **v9.1** 用户批准/拒绝方案 |
| `tool` | `sub_request` | `assistant` | ← | 启动后台任务 (子Agent / CLI tool) |
| `tool` | `sub_progress` | `system` | ← | 后台任务进度 |
| `tool` | `sub_result` | `system` | ← | 后台任务结果 |
| `sync_request` | — | `user` | → | **v9.1** 重连后拉取错过的消息 |
| `sync_complete` | — | `assistant` | ← | **v9.1** 服务端回放完毕 |
| `error` | `TASK_FAILED` | `system` | ← | 任务失败 |
| `error` | `MAX_ROUNDS` | `system` | ← | LLM 工具调用循环达到上限 |
| `error` | `INTERNAL_ERROR` | `system` | ← | 内部错误 |

---

## 二、消息详述

### 2.1 `message` — 用户消息 + LLM 回复

```json
// → 用户消息
{"type":"message","subType":"chat","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"content":"搜索transformer论文"}}

// ← LLM 思考过程 (流式，priorityKind=silent，done=false 表示继续，done=true 表示思考结束)
{"type":"message","subType":"thinking","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","msg_id":"...","priorityKind":"silent","payload":{"content":"用户想搜索...","done":false}}
{"type":"message","subType":"thinking","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","msg_id":"...","priorityKind":"silent","payload":{"content":"","done":true}}

// ← LLM 最终回复 (完整文本，priorityKind=high → 离线时触发 APNs)
{"type":"message","subType":"text","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","msg_id":"...","priorityKind":"high","payload":{"content":"## 搜索结果\n\n..."}}
```

### 2.2 `tool/ios_request` + `tool/ios_result` — iOS 工具

LLM 请求 iOS 执行工具（分享、拍照、定位等）。带 `id` 用于关联结果。

```json
// ← 请求 iOS 执行
{"type":"tool","subType":"ios_request","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"id":"call_1","name":"share","input":{"url":"https://..."}}}

// → iOS 返回结果
{"type":"tool","subType":"ios_result","role":"tool","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"tool_call_id":"call_1","content":{"success":true}}}
```

### 2.3 `tool/ask_user_question` — 向用户提问

LLM 通过 iOS 向用户提问（澄清意图、确认方案等）。`ask_user_question` 是内置的 iOS tool，LLM 调用它来获取用户输入。带 `id` 用于关联回答。

```json
// ← LLM 请求向用户提问
{"type":"tool","subType":"ask_user_question","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"id":"call_q1","questions":[{"id":"q1","question":"你关注哪个子方向？","type":"open"},{"id":"q2","question":"年份范围？","type":"single_choice","options":["近1年","近3年","近5年"]}],"context":"正在规划搜索方案"}}

// → 用户回答
{"type":"tool","subType":"ask_user_question","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"tool_call_id":"call_q1","answers":[{"id":"q1","answer":"attention mechanism"},{"id":"q2","answer":"近3年"}]}}
```

### 2.4 `tool/propose_plan` — 方案审批（v9.1 新增）

MainAgent 在 `scenario_plan` 节点判定 `needs_approval=true` 时（如涉及大量下载/敏感操作）发出。iOS 应渲染**结构化 plan 卡片** + 权限列表，让用户一键批准或拒绝。

```json
// ← 待审批方案
{"type":"tool","subType":"propose_plan","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","priorityKind":"high","payload":{
  "id": "plan-call-xxx",
  "scenario_id": "S2",
  "summary": "将搜索 50 篇关于 self-supervised learning 的论文，下载/转换/索引后生成 markdown 综述。",
  "permissions": ["search", "download"],
  "estimated_time_seconds": 1200,
  "tools": [
    {"call_id":"c1","kind":"sub_agent","name":"ingest","arguments":{"query":"self-supervised learning","max_results":50}, "depends_on":[]}
  ]
}}

// → 用户批准 / 拒绝
{"type":"tool","subType":"propose_plan","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"tool_call_id":"plan-call-xxx","approved":true}}
// 或拒绝
{"type":"tool","subType":"propose_plan","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"tool_call_id":"plan-call-xxx","approved":false,"reason":"先不下载，只列表"}}
```

`tools[]` 的字段对应 [main_agent_prompts.py](../../src/paper_search/agent/main_agent_prompts.py) 的 `ToolCallSpec`。

### 2.5 `tool/sub_*` — 后台任务 (子Agent / CLI tool)

`sub_request` 启动后台任务。`sub_progress` 推送进度。`sub_result` 推送最终结果。三种消息共享同一个 `taskId` 做关联。

```json
// ← 启动后台任务
{"type":"tool","subType":"sub_request","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","priorityKind":"high","payload":{"taskId":"task-001","name":"ingest","label":"论文搜索入库","query":"transformer attention","estimatedStages":7}}

// ← 进度推送 (priorityKind=normal, 多条)
{"type":"tool","subType":"sub_progress","role":"system","agentId":"agent-001","sessionId":"main","timestamp":"...","priorityKind":"normal","payload":{"taskId":"task-001","name":"ingest","stage":"download","current":5,"total":20,"message":"正在下载 5/20"}}

// ← 最终结果 (priorityKind=high, 触发 APNs)
{"type":"tool","subType":"sub_result","role":"system","agentId":"agent-001","sessionId":"main","timestamp":"...","priorityKind":"high","payload":{"taskId":"task-001","name":"ingest","status":"done","summary":"找到20篇，下载15篇，索引完成","result":{"totalPapers":20,"downloaded":15}}}
```

### 2.6 `sync_request` / `sync_complete` — 离线消息同步（v9.1 新增）

iOS 重连后**应主动**发送 `sync_request`，服务端会回放本 session 未送达的消息（24h 内、priorityKind ≠ silent、同 taskId 进度去重保最后一条）。

```json
// → iOS 请求同步
{"type":"sync_request","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"last_msg_id":"abc-def-..."}}
// last_msg_id 可省略；省略时返回 24h 内全部未送达消息

// ← 服务端逐条回放 (含 msg_id, iOS 用于排重)
{"type":"tool","subType":"sub_result","role":"system","msg_id":"...","payload":{...}}
{"type":"message","subType":"text","role":"assistant","msg_id":"...","payload":{...}}

// ← 完成信标
{"type":"sync_complete","role":"assistant","agentId":"agent-001","sessionId":"main","payload":{"synced_count":12}}
```

### 2.7 `error` — 错误

```json
{"type":"error","subType":"TASK_FAILED","role":"system","priorityKind":"urgent","payload":{"taskId":"task-001","message":"搜索超时","recoverable":true}}
{"type":"error","subType":"MAX_ROUNDS","role":"system","priorityKind":"high","payload":{"message":"工具调用循环达到上限 (20 轮)，自动终止","recoverable":true}}
{"type":"error","subType":"INTERNAL_ERROR","role":"system","priorityKind":"urgent","payload":{"message":"LLM 调用失败","recoverable":true}}
```

---

## 三、典型对话流程

```
→ ping
← pong

→ message/chat                         {"content":"做一个 self-supervised learning 的综述"}
← message/thinking                     {"content":"判定为综述生成场景...","done":false}
← message/thinking                     {"content":"","done":true}
← tool/ask_user_question               {"id":"q1","questions":[{"question":"想要 50 篇还是 150 篇？"}]}

→ tool/ask_user_question               {"tool_call_id":"q1","answers":[{"answer":"50"}]}

← message/thinking                     {"content":"","done":true}
← tool/propose_plan                    {"id":"plan-1","summary":"将搜索+下载+综述","permissions":["search","download"],"tools":[...]}

→ tool/propose_plan                    {"tool_call_id":"plan-1","approved":true}

← tool/sub_request                     {"taskId":"t1","name":"ingest","query":"self-supervised learning"}
← tool/sub_progress                    {"taskId":"t1","stage":"search","current":15,"total":20}
← tool/sub_progress                    {"taskId":"t1","stage":"download","current":3,"total":15}
← tool/sub_result                      {"taskId":"t1","status":"done","summary":"完成"}
← message/thinking                     {"content":"评估满足度，整理结果","done":true}
← message/text                         {"content":"## 综述生成完毕\n\n..."}
```

---

## 四、APNs 离线推送规则（v9.1 新增）

离线时（无任何活跃 WS 连接），服务端会按 `priorityKind` 决定是否触发 APNs：

| priorityKind | 在线 | 离线 |
|---|---|---|
| `silent` | WS 推 | **丢弃** |
| `normal` | WS 推 + 持久化 | 持久化（不 APNs） |
| `high` | WS 推 + 持久化 | 持久化 + APNs（带预览） |
| `urgent` | WS 推 + 持久化 | 持久化 + APNs（带响铃） |

iOS 启动时应通过 `POST /api/devices/register` 注册 APNs device token：

```http
POST /api/devices/register
{
  "agent_id": "agent-001",
  "device_token": "<hex APNs token>",
  "platform": "ios",
  "bundle_id": "com.example.PaperAgent"
}
```

---

## 五、和旧版差异

| | v7.0 | v9.0 | v9.1 (current) |
|------|------|------|------|
| 握手 | `message(chat) seq=1` → `phase(connected)` | 无，ping/pong | 同 v9.0；重连后建议发 sync_request |
| `seq` / `priority` (int) | 有 | 移除 | 移除 |
| `msg_id` / `priorityKind` | 无 | 无 | **新增** |
| `review` | clarify/plan/task_control | 移除 → `tool/ask_user_question` | 同 v9.0；额外新增 `tool/propose_plan` |
| `phase` | connected/clarify/planning/execute/done | 移除 | 移除 |
| iOS 工具 | `tool/ios_request` + `tool/result` | `tool/ios_request` + `tool/ios_result` | 同 v9.0 |
| 子Agent | `phase/progress` | `tool/sub_request` + `tool/sub_progress` + `tool/sub_result` | 同 v9.0 |
| 用户提问 | `review/clarify` | `tool/ask_user_question` | 同 v9.0 |
| 离线消息 | 无机制（丢失） | 无机制（丢失） | **outbox 持久化 + sync_request 回放 + APNs** |
| 方案审批 | `review/plan` | 用 `ask_user_question` 凑合 | **`tool/propose_plan` 结构化卡片** |

