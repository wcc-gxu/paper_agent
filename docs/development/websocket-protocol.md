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
| `agent_id` | Agent 部署实例标识。默认 `"agent-001"` |
| `session_id` | 会话标识。默认 `"main"` |

### 1.2 连接建立

**无握手协议。** iOS 连接成功后立即发送 ping，服务端回复 pong，即开始通信。

```
iOS → Server:  WebSocket 连接
iOS → Server:  {"type": "ping"}
Server → iOS:  {"type": "pong"}
              (开始收发业务消息)
```

服务端 **永不主动断开连接**。异常时自动重连。

### 1.3 通用信封

```json
{
  "type": "<大类>",
  "subType": "<子类>",
  "sessionId": "main",
  "timestamp": "2026-06-20T10:30:00Z",
  "payload": { ... }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 消息大类: `ping` / `pong` / `phase` / `message` / `tool` / `review` / `error` |
| `subType` | string | 消息子类 |
| `sessionId` | string | 会话标识 |
| `timestamp` | string | ISO 8601 UTC 时间戳 |
| `payload` | object | 消息体 |

**v9.0 移除的字段**: `role`, `agentId`, `seq`, `priority`。

### 1.4 消息速查表

| type | subType | 方向 | 说明 |
|------|---------|:---:|------|
| `ping` | — | → | 心跳 |
| `pong` | — | ← | 心跳回复 |
| `message` | `chat` | → | 用户聊天消息 |
| `message` | `text` | ← | LLM 流式文本 token |
| `message` | `reply` | ← | LLM 最终回复 |
| `phase` | `clarify` | ← | 正在分析需求 |
| `phase` | `execute` | ← | 正在执行任务 |
| `phase` | `done` | ← | 本轮完成 |
| `review` | `clarify` | ← | 需要用户澄清问题 |
| `review` | `plan` | ← | 方案待确认 |
| `tool` | `ios_request` | ← | 请求 iOS 执行工具 |
| `tool` | `result` | → | iOS 工具执行结果 |
| `error` | `TASK_FAILED` | ← | 任务执行失败 |
| `error` | `INTERNAL_ERROR` | ← | 内部错误 |

---

## 二、消息详细定义

### 2.1 心跳

```
→ {"type": "ping"}
← {"type": "pong"}
```

iOS 连接后立即发 ping。之后每 30 秒发一次。

### 2.2 用户聊天消息

```json
→ {
  "type": "message",
  "subType": "chat",
  "payload": {
    "content": "帮我搜索transformer相关论文"
  }
}
```

### 2.3 LLM 流式文本

```json
← {
  "type": "message",
  "subType": "text",
  "payload": {
    "index": 0,
    "delta": "好的，我来",
    "done": false
  }
}
```

### 2.4 LLM 最终回复

```json
← {
  "type": "message",
  "subType": "reply",
  "payload": {
    "content": "## 研究结果\n\n..."
  }
}
```

### 2.5 用户澄清

```json
← {
  "type": "review",
  "subType": "clarify",
  "payload": {
    "message": "请确认以下问题",
    "questions": [
      {"id": "q1", "question": "你关注哪个子领域？"}
    ]
  }
}

→ {
  "type": "review",
  "subType": "clarify",
  "payload": {
    "answers": [{"id": "q1", "answer": "attention mechanism"}]
  }
}
```

### 2.6 方案确认

```json
← {
  "type": "review",
  "subType": "plan",
  "payload": {
    "goal": "搜索Transformer论文",
    "steps": [...]
  }
}

→ {
  "type": "review",
  "subType": "plan",
  "payload": {
    "confirmed": true,
    "taskId": "task-001"
  }
}
```

### 2.7 iOS 工具调用

```json
← {
  "type": "tool",
  "subType": "ios_request",
  "payload": {
    "name": "share",
    "input": {...}
  }
}

→ {
  "type": "tool",
  "subType": "result",
  "payload": {
    "tool_call_id": "call_123",
    "content": {...}
  }
}
```

---

## 三、架构说明

### 3.1 消息流

```
iOS ←→ API Server ←→ Redis ←→ Agent Daemon ←→ Celery Worker
      (WS relay)    (queue)    (AgentLoop)     (async tasks)
```

1. **API Server** — 纯 WebSocket 中继。收消息 LPUSH Redis，Daemon 回复通过 Pub/Sub 转发 WS
2. **Agent Daemon** — BRPOP Redis 队列 → LLM tool-calling loop → Pub/Sub 输出
3. **Celery Worker** — 执行子 Agent 异步任务

### 3.2 连接管理

- **服务端永不主动断开** — 异常时自动恢复
- **ping/pong 心跳** — iOS 每 30 秒发 ping，服务端立即回 pong
- **断线重连** — iOS 检测断开后立即重连，无需重新握手
