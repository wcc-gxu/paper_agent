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
| `role` | string | `user`=iOS发出, `assistant`=LLM/服务端发出, `tool`=工具结果, `system`=子Agent状态 |
| `agentId` | string | Agent 实例 ID |
| `sessionId` | string | 会话 ID |
| `timestamp` | string | ISO 8601 UTC |
| `payload` | object | 消息体 |

### 1.4 消息速查全表

| type | subType | role | →/← | payload | 说明 |
|------|---------|------|:---:|------|------|
| `ping` | — | `user` | → | `{}` | 心跳 |
| `pong` | — | `assistant` | ← | `{}` | 心跳回复 |
| `message` | `chat` | `user` | → | `{"content":"..."}` | 用户消息 |
| `message` | `text` | `assistant` | ← | `{"content":"## 完整回复\n..."}` | LLM 完整回复 |
| `message` | `thinking` | `assistant` | ← | `{"content":"让我想想...","done":false}` | LLM 思考过程流式 |
| `tool` | `ask_user_question` | `assistant` | ← | `{"id":"call_1","questions":[...]}` | LLM 请求向用户提问 |
| `tool` | `ask_user_question` | `user` | → | `{"tool_call_id":"call_1","answers":[...]}` | 用户回答问题 |
| `tool` | `ios_request` | `assistant` | ← | `{"id":"call_2","name":"share","input":{}}` | 请求 iOS 执行工具 |
| `tool` | `result` | `tool` | → | `{"tool_call_id":"call_2","content":{}}` | iOS 工具执行结果 |
| `tool` | `launch_sub_agent` | `assistant` | ← | `{"taskId":"...","agentType":"ingest","query":"..."}` | 启动子Agent |
| `tool` | `sub_agent_progress` | `system` | ← | `{"taskId":"...","agentType":"ingest","stage":"download","current":5,"total":20}` | 子Agent 进度 |
| `tool` | `sub_agent_result` | `system` | ← | `{"taskId":"...","agentType":"ingest","status":"done","result":{}}` | 子Agent 结果 |
| `error` | `TASK_FAILED` | `system` | ← | `{"taskId":"...","message":"..."}` | 任务失败 |
| `error` | `INTERNAL_ERROR` | `system` | ← | `{"message":"..."}` | 内部错误 |

---

## 二、消息详述

### 2.1 `message/chat` — 用户消息 (→)

```json
{
  "type": "message", "subType": "chat", "role": "user",
  "agentId": "agent-001", "sessionId": "main",
  "timestamp": "2026-06-20T12:00:00Z",
  "payload": {
    "content": "帮我搜索 transformer attention 相关论文"
  }
}
```

### 2.2 `message/text` — LLM 完整回复 (←)

LLM 完成全部推理后，一次性发送完整文本。不做流式拆分。

```json
{
  "type": "message", "subType": "text", "role": "assistant",
  "agentId": "agent-001", "sessionId": "main",
  "timestamp": "2026-06-20T12:01:00Z",
  "payload": {
    "content": "## 搜索结果\n\n找到以下相关论文：\n\n1. **Attention Is All You Need** (2017)..."
  }
}
```

### 2.3 `message/thinking` — LLM 思考过程 (←)

流式推送 LLM 推理过程。`done: true` 表示思考结束，`text` 即将到达。

```json
{
  "type": "message", "subType": "thinking", "role": "assistant",
  "agentId": "agent-001", "sessionId": "main",
  "payload": {
    "content": "用户想搜索 transformer 论文，我需要先澄清...",
    "done": false
  }
}
```

---

## 三、Tool 消息详述

### 3.1 `tool/ask_user_question` — LLM 提问用户 (←)

替代旧协议的 `review/clarify` 和 `review/plan`。LLM 通过这个 tool 向用户问任何问题（澄清意图、确认方案、审批权限）。

```json
{
  "type": "tool", "subType": "ask_user_question", "role": "assistant",
  "agentId": "agent-001", "sessionId": "main",
  "timestamp": "2026-06-20T12:00:30Z",
  "payload": {
    "id": "call_q1",
    "questions": [
      {"id": "q1", "question": "你关注 transformer 的哪个子方向？", "type": "text"},
      {"id": "q2", "question": "论文年份范围？", "type": "choice", "options": ["近1年","近3年","近5年"]}
    ],
    "context": "LLM 正在规划搜索方案，需要更多信息"
  }
}
```

### 3.2 `tool/ask_user_question` — 用户回答 (→)

```json
{
  "type": "tool", "subType": "ask_user_question", "role": "user",
  "agentId": "agent-001", "sessionId": "main",
  "timestamp": "2026-06-20T12:01:00Z",
  "payload": {
    "tool_call_id": "call_q1",
    "answers": [
      {"id": "q1", "answer": "attention mechanism"},
      {"id": "q2", "answer": "近3年"}
    ]
  }
}
```

### 3.3 `tool/ios_request` — 请求 iOS 执行 (←)

```json
{
  "type": "tool", "subType": "ios_request", "role": "assistant",
  "agentId": "agent-001", "sessionId": "main",
  "payload": {
    "id": "call_ios1",
    "name": "share",
    "input": {"url": "https://arxiv.org/abs/1706.03762"}
  }
}
```

### 3.4 `tool/result` — iOS 工具返回 (→)

```json
{
  "type": "tool", "subType": "result", "role": "tool",
  "agentId": "agent-001", "sessionId": "main",
  "payload": {
    "tool_call_id": "call_ios1",
    "content": {"success": true}
  }
}
```

### 3.5 `tool/launch_sub_agent` — 启动子 Agent (←)

```json
{
  "type": "tool", "subType": "launch_sub_agent", "role": "assistant",
  "agentId": "agent-001", "sessionId": "main",
  "payload": {
    "taskId": "task-20260620-001",
    "agentType": "ingest",
    "query": "transformer attention mechanism",
    "estimatedStages": 7
  }
}
```

### 3.6 `tool/sub_agent_progress` — 子 Agent 进度 (←)

```json
{
  "type": "tool", "subType": "sub_agent_progress", "role": "system",
  "agentId": "agent-001", "sessionId": "main",
  "payload": {
    "taskId": "task-20260620-001",
    "agentType": "ingest",
    "stage": "download",
    "current": 5,
    "total": 20,
    "message": "正在下载第 5/20 篇论文"
  }
}
```

### 3.7 `tool/sub_agent_result` — 子 Agent 结果 (←)

```json
{
  "type": "tool", "subType": "sub_agent_result", "role": "system",
  "agentId": "agent-001", "sessionId": "main",
  "payload": {
    "taskId": "task-20260620-001",
    "agentType": "ingest",
    "status": "done",
    "summary": "找到 20 篇论文，下载 15 篇，索引完成",
    "result": {
      "totalPapers": 20,
      "downloaded": 15,
      "indexed": 15,
      "surveyPath": "/path/to/survey.md"
    }
  }
}
```

---

## 四、典型对话流程

```
→ ping
← pong

→ message/chat                          {"content":"搜索transformer论文"}
← message/thinking                      {"content":"用户想搜索论文，需要确认方向...","done":false}
← message/thinking                      {"content":"应该询问子方向和年份范围","done":true}
← tool/ask_user_question                {"id":"q1","questions":[...]}

→ tool/ask_user_question                {"tool_call_id":"q1","answers":[...]}

← message/thinking                      {"content":"用户确认了方向，现在生成搜索方案","done":true}
← tool/launch_sub_agent                 {"taskId":"t1","agentType":"ingest","query":"..."}
← tool/sub_agent_progress               {"taskId":"t1","stage":"search","current":15,"total":20}
← tool/sub_agent_progress               {"taskId":"t1","stage":"download","current":3,"total":15}
← tool/sub_agent_result                 {"taskId":"t1","status":"done","summary":"完成","result":{...}}
← message/thinking                      {"content":"搜索完成，整理结果中","done":true}
← message/text                          {"content":"## 搜索结果\n\n找到15篇..."}
```

---

## 五、和 v7.0/v8.0 的差异

| | v7.0 | v9.0 |
|------|------|------|
| 握手 | `message(chat) seq=1` → `phase(connected)` | 无，直接 ping/pong |
| `seq` 字段 | 有 | 移除 |
| `review` 类型 | clarify / plan / task_control | 移除，统一用 `tool/ask_user_question` |
| `phase` 类型 | connected / clarify / planning / execute / done | 移除，状态由消息序列隐式表达 |
| LLM 输出 | `message/text` 流式 token | `message/thinking` 流式思考 + `message/text` 完整回复 |
| 子Agent | `phase/progress` | `tool/sub_agent_progress` + `tool/sub_agent_result` |
| `role` 字段 | 有 | 保留，增加 `tool` / `system` |
