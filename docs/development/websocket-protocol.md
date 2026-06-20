# Paper Agent v3 — WebSocket 通信协议

> iOS 客户端对接规范 | v9.0 | 2026-06-20

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

### 1.3 通用信封

```json
{
  "type": "message | tool | ping | pong | error",
  "subType": "<子类>",
  "role": "user | assistant | tool | system",
  "agentId": "agent-001",
  "sessionId": "main",
  "timestamp": "2026-06-20T12:00:00Z",
  "payload": {}
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息大类: `message` / `tool` / `ping` / `pong` / `error` |
| `subType` | string | 消息子类 |
| `role` | string | `user`=iOS发出, `assistant`=LLM/主Agent发出, `tool`=工具返回, `system`=后台任务状态 |
| `agentId` | string | Agent 实例 ID |
| `sessionId` | string | 会话 ID |
| `timestamp` | string | ISO 8601 UTC |
| `payload` | object | 消息体 |

### 1.4 消息速查全表

| type | subType | role | →/← | 说明 |
|------|---------|------|:---:|------|
| `ping` | — | `user` | → | 心跳 |
| `pong` | — | `assistant` | ← | 心跳回复 |
| `message` | `chat` | `user` | → | 用户聊天消息 |
| `message` | `text` | `assistant` | ← | LLM 完整回复 |
| `message` | `thinking` | `assistant` | ← | LLM 思考过程 (流式) |
| `tool` | `ios_request` | `assistant` | ← | 请求 iOS 执行工具 |
| `tool` | `ios_result` | `tool` | → | iOS 工具返回结果 |
| `tool` | `ask_user_question` | `assistant` | ← | 请求 iOS 向用户提问 |
| `tool` | `ask_user_question` | `user` | → | 用户回答 |
| `tool` | `sub_request` | `assistant` | ← | 启动后台任务 (子Agent / CLI tool) |
| `tool` | `sub_progress` | `system` | ← | 后台任务进度 |
| `tool` | `sub_result` | `system` | ← | 后台任务结果 |
| `error` | `TASK_FAILED` | `system` | ← | 任务失败 |
| `error` | `INTERNAL_ERROR` | `system` | ← | 内部错误 |

---

## 二、消息详述

### 2.1 `message` — 用户消息 + LLM 回复

```json
// → 用户消息
{"type":"message","subType":"chat","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"content":"搜索transformer论文"}}

// ← LLM 思考过程 (流式，done=false 表示继续，done=true 表示思考结束)
{"type":"message","subType":"thinking","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"content":"用户想搜索...","done":false}}
{"type":"message","subType":"thinking","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"content":"","done":true}}

// ← LLM 最终回复 (完整文本)
{"type":"message","subType":"text","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"content":"## 搜索结果\n\n..."}}
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
{"type":"tool","subType":"ask_user_question","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"id":"call_q1","questions":[{"id":"q1","question":"你关注哪个子方向？"},{"id":"q2","question":"年份范围？","type":"choice","options":["近1年","近3年","近5年"]}],"context":"正在规划搜索方案"}}

// → 用户回答
{"type":"tool","subType":"ask_user_question","role":"user","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"tool_call_id":"call_q1","answers":[{"id":"q1","answer":"attention mechanism"},{"id":"q2","answer":"近3年"}]}}
```

### 2.4 `tool/sub_*` — 后台任务 (子Agent / CLI tool)

`sub_request` 启动后台任务。`sub_progress` 推送进度。`sub_result` 推送最终结果。三种消息共享同一个 `taskId` 做关联。

```json
// ← 启动后台任务
{"type":"tool","subType":"sub_request","role":"assistant","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"taskId":"task-001","name":"ingest","label":"论文搜索入库","query":"transformer attention","estimatedStages":7}}

// ← 进度推送
{"type":"tool","subType":"sub_progress","role":"system","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"taskId":"task-001","name":"ingest","stage":"download","current":5,"total":20,"message":"正在下载 5/20"}}

// ← 最终结果
{"type":"tool","subType":"sub_result","role":"system","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"taskId":"task-001","name":"ingest","status":"done","summary":"找到20篇，下载15篇，索引完成","result":{"totalPapers":20,"downloaded":15}}}
```

### 2.5 `error` — 错误

```json
{"type":"error","subType":"TASK_FAILED","role":"system","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"taskId":"task-001","message":"搜索超时","recoverable":true}}
{"type":"error","subType":"INTERNAL_ERROR","role":"system","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{"message":"LLM 调用失败","recoverable":true}}
```

---

## 三、典型对话流程

```
→ ping
← pong

→ message/chat                         {"content":"搜索transformer论文"}
← message/thinking                     {"content":"需要确认方向...","done":false}
← message/thinking                     {"content":"","done":true}
← tool/ask_user_question               {"id":"q1","questions":[...]}

→ tool/ask_user_question               {"tool_call_id":"q1","answers":[...]}

← message/thinking                     {"content":"方案生成完毕","done":true}
← tool/sub_request                     {"taskId":"t1","name":"ingest","query":"..."}
← tool/sub_progress                    {"taskId":"t1","stage":"search","current":15,"total":20}
← tool/sub_progress                    {"taskId":"t1","stage":"download","current":3,"total":15}
← tool/sub_result                      {"taskId":"t1","status":"done","summary":"完成"}
← message/thinking                     {"content":"整理结果中","done":true}
← message/text                         {"content":"## 搜索结果\n\n找到15篇..."}
```

---

## 四、和旧版差异

| | v7.0 | v9.0 |
|------|------|------|
| 握手 | `message(chat) seq=1` → `phase(connected)` | 无，ping/pong |
| `seq` / `priority` | 有 | 移除 |
| `review` | clarify/plan/task_control | 移除 → `tool/ask_user_question` |
| `phase` | connected/clarify/planning/execute/done | 移除 |
| iOS 工具 | `tool/ios_request` + `tool/result` | `tool/ios_request` + `tool/ios_result` |
| 子Agent | `phase/progress` | `tool/sub_request` + `tool/sub_progress` + `tool/sub_result` |
| 用户提问 | `review/clarify` | `tool/ask_user_question` |
