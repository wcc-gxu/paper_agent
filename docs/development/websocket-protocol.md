# Paper Agent v3 — WebSocket 通信协议

> iOS 客户端对接规范 | 2026-06-16

---

## 一、总纲

### 1.1 WebSocket 地址

```
ws://{host}:{port}/ws/chat/{agent_id}/{session_id}
```

| 参数 | 说明 |
|------|------|
| `agent_id` | Agent 部署实例标识。每个 Agent 独立进程、端口、DB。默认 `"agent-001"` |
| `session_id` | 会话标识。一期固定 `"main"` |

### 1.2 agent_id 与 session_id

一期每个 Agent 只有默认会话。多 Agent 通过不同端口部署独立 daemon 进程实现隔离。

```
agent-001 :8000  →  session: "main"
agent-cv  :8001  →  session: "main"
agent-nlp :8002  →  session: "main"
```

| 概念 | 生命周期 | 隔离内容 |
|------|----------|----------|
| `agent_id` — 部署实例 | 永久 | 独立进程、DB、Chromadb、PlanGraph checkpoint |
| `session_id` — 固定 "main" | 一期唯一 | 所有对话上下文 |

> **多 session 和多 Agent 的完整设计见 [agent-runloop.md](agent-runloop.md)。** 一期 Agent 内不区分 session，所有对话共享 ShortTerm。

### 1.3 通用信封

所有消息共用此结构。**iOS 端和 Server 端均需持久化全部字段**。

```json
{
  "role": "user | assistant",
  "type": "<大类>",
  "subType": "<子类>",
  "agentId": "agent-001",
  "sessionId": "main",
  "seq": 0,
  "priority": 0 | 1 | 2,
  "timestamp": "2026-06-16T10:30:00Z",
  "payload": { ... }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `role` | string | `user` = 客户端发出，`assistant` = 服务端发出。**持久化必填** |
| `type` | string | **消息大类**，共 8 种：`heartbeat` / `phase` / `thinking` / `message` / `tool` / `review` / `error` / `task` |
| `subType` | string | **消息子类**，在大类下细分具体行为。见 §1.5 速查表 |
| `agentId` | string | Agent ID。**持久化必填** |
| `sessionId` | string | 会话 ID。**持久化必填** |
| `seq` | int | **仅 `role:"user"` 的消息需要。** 每个 session 的第一条消息必须 `seq=1`，之后严格递增。新 seq > 当前处理中 seq → Server 放弃旧消息。见下文 |
| `priority` | int | `0`=流式/状态 `1`=普通业务 `2`=阻塞等用户。**Server 据此决定 APNs 推送** |
| `timestamp` | string | ISO 8601 UTC。**持久化必填** |
| `payload` | object | 消息体，按 `type`+`subType` 不同（见 §2） |

**握手协议**：

```
① iOS → Server:  WebSocket 连接 /ws/chat/{agent_id}/{session_id}
② iOS → Server:  message(chat, seq=1)               ← 连接后发送的第一条消息，必须夹带握手信息，seq=1
③ Server:        检查 session 是否存在
                    ├── 存在 → 从 checkpoint 恢复上下文
                    └── 不存在 → 自动创建 session
④ Server → iOS:  phase(connected, seq=1)             ← 握手回复，echo seq=1，含 session 详情
⑤ 握手完成，进入正常交互
```

此后每条 `message(chat)` 的 seq=0，表示不是握手信息。

### 1.4 priority 与 APNs 推送

| priority | 含义 | APNs | 典型消息 |
|----------|------|------|----------|
| `2` | 阻塞性 — 没有用户指令系统无法继续 | ✅ **立即推送**，每条独立 | review、tool(ios)、error、plan_rejected |
| `1` | 普通业务 — 有意义但不紧急 | ✅ **合并推送**（≤10min 一条） | message(chat/reply/notification)、tool(result)、task |
| `0` | 流式/状态/心跳 — 仅当前连接有效 | ❌ **不推送** | thinking、message(text)、tool(server)、phase、heartbeat |

### 1.5 消息类型速查表（8 大类 × 27 种子类）

| role | type | subType | priority | 用途 | 详述 |
|------|------|---------|----------|------|------|
| user | `heartbeat` | `ping` | 0 | 心跳（每 30s） | §1.6 |
| assistant | `heartbeat` | `pong` | 0 | 心跳回复 | §1.6 |
| assistant | `phase` | `connected` | 0 | Session 就绪 | §2.1 |
| assistant | `phase` | `clarify` | 0 | 分析需求中 | §2.1 |
| assistant | `phase` | `plan` | 0 | 方案已生成 | §2.1 |
| assistant | `phase` | `execute` | 0 | 执行中 | §2.1 |
| assistant | `phase` | `verify` | 0 | 验证结果 | §2.1 |
| assistant | `phase` | `summarize` | 0 | 生成报告 | §2.1 |
| assistant | `phase` | `paused` | 0 | 已暂停 | §2.1 |
| assistant | `phase` | `recovering` | 0 | 崩溃恢复 | §2.1 |
| assistant | `phase` | `done` | 0 | 全部完成 | §2.1 |
| assistant | `thinking` | — | 0 | LLM 流式思考 | §2.2 |
| assistant | `message` | `text` | 0 | 流式文本 | §2.3 |
| user | `message` | `chat` | 1 | 用户输入 | §2.3 |
| assistant | `message` | `reply` | 1 | 最终回复 | §2.3 |
| assistant | `message` | `title` | 1 | 标题变更 | §2.3 |
| assistant | `message` | `notification` | 1 | 后台通知 | §2.3 |
| assistant | `message` | `plan_rejected` | 2 | Plan 被拒 | §2.3 |
| assistant | `tool` | `server` | 0 | Server 工具展示 | §2.4 |
| assistant | `tool` | `ios` | 2 | 请求 iOS 执行 | §2.4 |
| user | `tool` | `result` | 1 | iOS 工具结果 | §2.4 |
| assistant | `review` | `clarify` | 2 | 澄清请求 | §2.5 |
| user | `review` | `clarify` | 2 | 澄清回复 | §2.5 |
| assistant | `review` | `plan` | 2 | 计划请求 | §2.5 |
| user | `review` | `plan` | 2 | 计划回复 | §2.5 |
| user | `review` | `task_control` | 2 | 暂停/恢复/取消 | §2.5 |
| assistant | `error` | _错误码_ | 2 | 错误通知 | §2.6 |
| assistant | `task` | `started` | 1 | 任务创建 | §2.7 |
| assistant | `task` | `running` | 1 | 任务进度 | §2.7 |
| assistant | `task` | `backgrounded` | 1 | 转入后台（不可逆） | §2.7 |
| assistant | `task` | `done` | 1 | 任务完成 | §2.7 |
| assistant | `task` | `failed` | 1 | 任务失败 | §2.7 |

### 1.6 心跳

```json
// C→S 每 30s
{"role":"user","type":"heartbeat","subType":"ping","agentId":"agent-001","sessionId":"main","priority":0,"timestamp":"...","payload":{}}

// S→C 收到立即回复
{"role":"assistant","type":"heartbeat","subType":"pong","agentId":"agent-001","sessionId":"main","priority":0,"timestamp":"...","payload":{}}
```

客户端 90s 内未收到任何消息（包括 pong）→ 断开重连。

---

## 二、消息类型详述

> 按 7 大类展开。S = Server，C = iOS Client。

### 2.1 phase — 阶段切换

**role: assistant** | priority: 0（不推送）

`subType` 即为当前阶段名。连接确认也是一种阶段。

```
iOS 连接 WS → 立即发 message(chat)（不等 Server）
Server 收到首条 chat → phase(subType:"connected") → 处理消息 → 后续 phase 通知
```

```json
// 连接确认
{
  "role": "assistant", "type": "phase", "subType": "connected",
  "agentId": "agent-001", "sessionId": "main", "priority": 0,
  "timestamp": "2026-06-16T10:30:00Z",
  "payload": {
    "sessionTitle": "Transformer 文献调研",
    "historyCount": 42,
    "activeTasks": [
      {"taskId": "task-001", "name": "入库 Transformer", "mode": "background",
       "stage": "下载论文", "current": 12, "total": 22, "status": "running"}
    ]
  }
}

// 执行中进度
{
  "role": "assistant", "type": "phase", "subType": "execute",
  "agentId": "agent-001", "sessionId": "main", "priority": 0,
  "timestamp": "2026-06-16T10:31:00Z",
  "payload": {
    "stage": "搜索论文",
    "stageIndex": 1,
    "totalStages": 6,
    "message": "正在搜索 Semantic Scholar...",
    "detail": {"papersFound": 18, "papersTotal": 30}
  }
}
```

| payload 字段 | 说明 |
|-------------|------|
| `sessionTitle` | 仅 `connected`。`null` = 新 session（标题待 LLM 生成） |
| `historyCount` | 仅 `connected`。历史消息数 |
| `activeTasks` | 仅 `connected`。进行中任务 |
| `stage` / `stageIndex` / `totalStages` | 当前步骤信息 |
| `message` | 人类可读的进度描述 |
| `detail` | 结构化进度（可选） |

**subType 枚举与 iOS UI**：

| subType | 含义 | iOS UI 建议 |
|---------|------|------------|
| `connected` | Session 就绪 | 恢复对话 UI |
| `clarify` | 分析需求 | 思考动画 |
| `plan` | 方案已生成 | —（内容通过 `review` 下发） |
| `execute` | 执行中 | 进度条 + 当前步骤 |
| `verify` | 验证结果 | "验证中..." |
| `summarize` | 生成报告 | 生成动画 |
| `paused` | 已暂停 | 暂停状态 + 继续按钮 |
| `recovering` | 崩溃恢复 | "恢复中..." spinner |
| `done` | 全部完成 | 结果摘要 |

> **Session 自动创建**：iOS 连接不存在的 session → 收到首条 chat 时 Server 自动创建 → `phase(connected, sessionTitle:null)` → LLM 生成标题 → `message(title)`。

### 2.2 thinking — 流式思考

**role: assistant** | **priority: 0**（不推送） | 无 subType

```json
{
  "role": "assistant", "type": "thinking",
  "agentId": "agent-001", "sessionId": "main", "priority": 0,
  "timestamp": "2026-06-16T10:30:01Z",
  "payload": {
    "delta": "我需要先分析用户的意图...",
    "done": false
  }
}
```

| payload | 说明 |
|---------|------|
| `delta` | 当前思考 token |
| `done` | `true` = 思考块结束 |

> iOS 折叠展示，默认收起。重连时未完成流丢弃。

### 2.3 message — 内容消息（双向）

**role: assistant 或 user** | priority: 0/1/2（按 subType）

**iOS 只需一个 `onMessage` 处理器，根据 `subType` 分发。**

```json
// ===== priority 0：流式 =====
// 流式文本
{
  "role": "assistant", "type": "message", "subType": "text",
  "agentId": "agent-001", "sessionId": "main", "priority": 0,
  "timestamp": "2026-06-16T10:30:01Z",
  "payload": {"index": 0, "delta": "我来", "done": false}
}
// done=true → 文本块结束
{"role":"assistant","type":"message","subType":"text","agentId":"agent-001","sessionId":"main","priority":0,
 "timestamp":"...","payload":{"index":0,"delta":"","done":true}}

// ===== priority 1：普通业务 =====
// 用户输入
{
  "role": "user", "type": "message", "subType": "chat",
  "agentId": "agent-001", "sessionId": "main", "seq": 1, "priority": 1,
  "timestamp": "2026-06-16T10:30:00Z",
  "payload": {
    "content": "帮我搜索 Transformer 注意力机制的论文",
    "ios_tools": [{"name":"share_sheet","description":"打开系统分享菜单","parameters":{...}}]
  }
}

// 最终回复
{
  "role": "assistant", "type": "message", "subType": "reply",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "timestamp": "2026-06-16T10:35:00Z",
  "payload": {
    "content": "## 调研结果\n\n找到 48 篇论文，22 篇高相关...",
    "files": [{"name":"survey.md","path":"/papers/outputs/proj-xxx/survey.md","size_bytes":45200}]
  }
}

// 标题变更
{
  "role": "assistant", "type": "message", "subType": "title",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "timestamp": "2026-06-16T10:30:05Z",
  "payload": {"title": "Transformer文献调研"}
}

// 后台通知（任务完成等）
{
  "role": "assistant", "type": "message", "subType": "notification",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "timestamp": "2026-06-16T12:00:00Z",
  "payload": {
    "title": "入库完成",
    "body": "Transformer 文献调研：48 篇论文，22 篇高相关已入库",
    "category": "task_complete",
    "data": {"taskId": "task-20260616-001"}
  }
}

// ===== priority 2：阻塞 =====
// Plan 被拒绝
{
  "role": "assistant", "type": "message", "subType": "plan_rejected",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:30:20Z",
  "payload": {"taskId": "task-20260616-001", "reason": "请重新制定计划"}
}
```

| subType | priority | role | 说明 | payload 关键字段 |
|---------|----------|------|------|-----------------|
| `text` | 0 | assistant | 流式/全文文本 | `index`, `delta`, `done` |
| `chat` | 1 | user | 用户输入 | `content`, `ios_tools` |
| `reply` | 1 | assistant | 最终回复 | `content`, `files` |
| `title` | 1 | assistant | 标题变更 | `title` |
| `notification` | 1 | assistant | 后台任务通知 | `title`, `body`, `category`, `data` |
| `plan_rejected` | 2 | assistant | Plan 被拒 | `taskId`, `reason` |

> **seq**：仅 `chat` 需要，递增。新 seq → Server 放弃当前处理。
> **流式中断**：重连时丢弃未完成的 `text` 流。LLM 完成后直接发 `reply` 带完整文本。

### 2.4 tool — 工具调用（双向）

**role: assistant 或 user** | priority: 0/1/2（按 subType）

```json
// ===== priority 0：Server 工具展示 =====
{
  "role": "assistant", "type": "tool", "subType": "server",
  "agentId": "agent-001", "sessionId": "main", "priority": 0,
  "timestamp": "2026-06-16T10:30:03Z",
  "payload": {
    "id": "call_s1",
    "name": "search_papers",
    "input": {"keywords": "transformer attention", "sources": "arxiv,semantic_scholar"},
    "status": "running"
  }
}
// 同 id 多次推送：status running → done/failed。iOS 据此 spinner→✓/✗

// ===== priority 2：请求 iOS 执行 =====
{
  "role": "assistant", "type": "tool", "subType": "ios",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:30:03Z",
  "payload": {
    "id": "call_x1",
    "name": "share_sheet",
    "input": {"text": "推荐论文: Attention Is All You Need", "url": "https://arxiv.org/abs/1706.03762"},
    "timeout": 30
  }
}

// ===== priority 1：iOS 工具结果 =====
// 成功
{
  "role": "user", "type": "tool", "subType": "result",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "timestamp": "2026-06-16T10:30:05Z",
  "payload": {"tool_call_id": "call_x1", "content": "已分享到微信"}
}
// 失败
{
  "role": "user", "type": "tool", "subType": "result",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "timestamp": "2026-06-16T10:30:05Z",
  "payload": {"tool_call_id": "call_x1", "error": "用户取消"}
}
```

| subType | priority | role | payload 关键字段 |
|---------|----------|------|-----------------|
| `server` | 0 | assistant | `id`, `name`, `input`, `status` |
| `ios` | 2 | assistant | `id`, `name`, `input`, `timeout` |
| `result` | 1 | user | `tool_call_id`, `content` / `error` |

> **重连时**：`server` 不回放历史，改为发 `phase` 带当前进度。

### 2.5 review — 审批（双向）

**role: assistant（请求）/ user（回复）** | **priority: 2**（立即推送）

一个 `review` 类型承载所有"暂停等用户"的交互。`subType` + `role` 完整区分场景。

```json
// ===== Server → iOS：审批请求 =====
// 澄清问题
{
  "role": "assistant", "type": "review", "subType": "clarify",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:30:05Z",
  "payload": {
    "message": "为了更准确地搜索，请确认以下问题：",
    "questions": [
      {"id": "q1", "question": "关注 AI 安全还是功能安全？", "options": ["AI 安全", "功能安全", "两者"]},
      {"id": "q2", "question": "时间范围？", "options": ["近1年", "近3年", "近5年", "不限"]}
    ]
  }
}

// 执行计划
{
  "role": "assistant", "type": "review", "subType": "plan",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:30:15Z",
  "payload": {
    "taskId": "task-20260616-001",
    "goal": "Transformer 注意力机制 AI 安全方向文献调研",
    "summary": "分 3 次搜索，预计找到 40-60 篇论文",
    "steps": [
      {"index": 1, "action": "search", "description": "搜索 adversarial attack on attention", "max_papers": 30},
      {"index": 2, "action": "search", "description": "搜索 transformer robustness verification", "max_papers": 20},
      {"index": 3, "action": "evaluate", "description": "LLM 评估相关性"},
      {"index": 4, "action": "download", "description": "下载高相关论文 PDF"},
      {"index": 5, "action": "convert_index", "description": "转换并索引入库"},
      {"index": 6, "action": "survey", "description": "生成文献综述"}
    ],
    "markdown": "## 研究方案\n\n..."
  }
}

// ===== iOS → Server：审批回复 =====
// 回答澄清
{
  "role": "user", "type": "review", "subType": "clarify",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:30:10Z",
  "payload": {
    "answers": [
      {"question_id": "q1", "answer": "AI 安全"},
      {"question_id": "q2", "answer": "近3年"}
    ]
  }
}

// 确认/拒绝/修改计划
{
  "role": "user", "type": "review", "subType": "plan",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "payload": {
    "taskId": "task-20260616-001",
    "confirmed": true,
    "modifications": {"steps": [{"index": 1, "max_papers": 50}]}
  }
}

// 任务控制（替代旧版 task_control）
{
  "role": "user", "type": "review", "subType": "task_control",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "payload": {"taskId": "task-20260616-001", "action": "pause"}
}
```

| subType | role（请求） | role（回复） | payload 关键字段 |
|---------|------------|------------|-----------------|
| `clarify` | assistant → `questions[]` | user ← `answers[]` | 请求: `message`, `questions` / 回复: `answers` |
| `plan` | assistant → `steps[]` | user ← `confirmed` | 请求: `taskId`, `goal`, `steps`, `markdown` / 回复: `taskId`, `confirmed`, `modifications?`, `reason?` |
| `task_control` | — | user → `action` | `taskId`, `action`（pause/resume/cancel） |

| action | 说明 |
|--------|------|
| `pause` | 当前阶段完成后暂停 |
| `resume` | 恢复已暂停任务 |
| `cancel` | 取消任务 |

Server 确认控制后通过 `phase(paused)` 或 `message(reply)` 反馈。

> **重连时**：未过期（<30min）的 Server review 重新发送。过期忽略。
> **被拒后**：Server 发 `message(plan_rejected, priority:2)`，立即推送。

### 2.6 error — 错误

**role: assistant** | **priority: 2**（立即推送） | subType = 错误码

```json
{
  "role": "assistant", "type": "error", "subType": "TOOL_TIMEOUT",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:32:00Z",
  "payload": {
    "message": "iOS 工具 'share_sheet' 在 30s 内未响应",
    "toolCallId": "call_abc123",
    "recoverable": true
  }
}
```

| subType | 说明 | recoverable |
|---------|------|-------------|
| `TOOL_TIMEOUT` | iOS 工具超时 | true |
| `TOOL_EXPIRED` | tool_result 到达太晚 | true |
| `TOOL_NOT_FOUND` | iOS 声明的工具不存在 | true |
| `SESSION_EXPIRED` | session 无效 | false（需重连） |
| `TASK_FAILED` | 任务执行失败 | true |
| `INTERNAL_ERROR` | 服务端内部错误 | false |
| `AGENT_NOT_FOUND` | agent_id 不存在 | false |
| `RATE_LIMITED` | 请求速率限制 | true |

> **重连时**：error 全部回放。

### 2.7 task — 后台任务（单向）

**role: assistant** | **priority: 1**（合并推送）

`task` 是独立的第 8 种消息类型。驱动 iOS 聊天列表的任务进度卡片。

**mode 状态机**：
```
foreground ──→ background   （用户发新消息时自动触发）
   │               │
   │               │  不可逆：background 不能切回 foreground
   │               │
   └─── done ──────┘
   └─── failed
```

```json
// 任务创建
{
  "role": "assistant", "type": "task", "subType": "started",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "payload": {
    "taskId": "task-20260616-001",
    "name": "入库 Transformer 安全方向",
    "mode": "foreground",
    "totalStages": 7
  }
}

// 进度更新（前台/后台通用）
{
  "role": "assistant", "type": "task", "subType": "running",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "payload": {
    "taskId": "task-20260616-001",
    "mode": "foreground",
    "stage": "下载论文",
    "stageIndex": 3,
    "totalStages": 7,
    "current": 34,
    "total": 50
  }
}

// 转入后台（不可逆）
{
  "role": "assistant", "type": "task", "subType": "backgrounded",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "payload": {
    "taskId": "task-20260616-001",
    "reason": "user_new_message"
  }
}

// 任务完成
{
  "role": "assistant", "type": "task", "subType": "done",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "payload": {
    "taskId": "task-20260616-001",
    "result": {"totalPaper": 50, "downloaded": 48, "failed": 2}
  }
}

// 任务失败
{
  "role": "assistant", "type": "task", "subType": "failed",
  "agentId": "agent-001", "sessionId": "main", "priority": 1,
  "payload": {
    "taskId": "task-20260616-001",
    "error": "下载阶段：3 篇论文 PDF 不可获取"
  }
}
```

| subType | mode 影响 | payload 关键字段 |
|---------|----------|-----------------|
| `started` | 携带初始 mode | `taskId`, `name`, `mode`, `totalStages` |
| `running` | 不变 | `taskId`, `stage`, `stageIndex`, `current`, `total` |
| `backgrounded` | foreground→background | `taskId`, `reason` |
| `done` | 结束 | `taskId`, `result` |
| `failed` | 结束 | `taskId`, `error` |

> **不可逆**：`backgrounded` 一旦发送，`mode` 永久为 `background`。后续 `running` 消息不再携带 `mode: foreground`。
> **重连时**：`done` / `failed` 不回放。`running` / `backgrounded` 回放最近 20 条。
> **前台→后台触发条件**：用户在当前有前台任务时发送新 `message(chat)`。

---

## 三、连接生命周期

### 3.1 连接握手

```
iOS → Server:  WS 连接 /ws/chat/agent-001/main
iOS → Server:  message(chat, seq:1)              ← 立即发送，不等 Server

Server:
  1. 收到 chat → 检查 session（不存在则自动创建）
  2. 发送 phase(connected)                        ← 握手确认
  3. 处理 chat → LLM 响应
  4. 首条消息 → LLM 生成标题 → message(title)
```

### 3.2 状态机

| 状态 | WS 消息 | APNs |
|------|---------|------|
| `connected` | ✅ 所有 priority | ❌ |
| `disconnected` | ❌ | priority 2 立即推 / priority 1 合并推 |
| `reconnecting` | phase(recovering) | ❌ |

### 3.3 完整流程

```
iOS 连接 WS
    │
    ▼
iOS → message(chat, seq:1)                         ← 立即发送
    │
    ▼
Server: 检查有效性
    │  ├── agent 不存在 → error(AGENT_NOT_FOUND)
    │  ├── session 无效 → error(SESSION_EXPIRED)
    │  └── 有效 → phase(connected)                  ← 握手
    │
    ▼
Server: 检查历史
    │  ├── 未完成任务 → phase(recovering) → 恢复
    │  └── 未完成流式 → 丢弃 → LLM 完成后 message(reply)
    │
    ▼
正常交互: message(chat) ↔ thinking / message(text) / tool / review / phase
    │
    ▼
iOS 断开 → Server 继续执行 → 按 priority 分流:
    priority 2 → APNs 立即推送
    priority 1 → APNs 合并推送 + Redis 缓存
    priority 0 → 丢弃
    │
    ▼
前台任务（用户在等）→ task(backgrounded) → 缓存到 Redis
    │
    ▼
iOS 重连 → phase(connected) → 回放未完成的 task → 正常交互
```

### 3.4 重连事件处理

| 事件 | 处理 |
|------|------|
| 未完成的 `thinking` / `message(text)` 流 | 丢弃。LLM 完成后发 `message(reply)` |
| `tool(server)` 历史 | 不回放。发 `phase` 带当前进度 |
| `review`（Server→iOS，未过期 <30min） | 重新发送 |
| `error` | 全部回放 |
| 已完成任务 | 最近 1 条 `message(reply)`，其余合并通知 |
| `task(running/backgrounded)` | 回放最近 20 条（含当前进度） |
| `task(done/failed)` | 不回放 |

### 3.5 后台切换

```
进入后台 → ~30s 后 WS 断开 → priority 2 立即推 / priority 1 合并推
进入前台 → 重连 WS → 停止 APNs → §3.4 流程
```

### 3.6 崩溃恢复

```
崩溃: LangGraph checkpoint（AsyncSqliteSaver，<1 节点丢失）
       Celery task 继续执行（独立 Worker 进程不受影响）
       EventBus 进程内事件丢失（stream/thinking 丢弃，重连后 phase 重放状态）
重启: LangGraph 从 checkpoint 恢复 → Celery Worker 重入队
重连: phase(recovering) → 继续执行
```

---

## 四、一期内存隔离

一期每个 Agent 只有 `session: "main"`。多任务通过 `taskId` 区分。

| 记忆层 | 存储 | 生命周期 |
|--------|------|----------|
| ShortTerm | 进程内存 | 当前对话窗口 |
| MidTerm | LangGraph checkpoint (SQLite) | 任务级，崩溃可恢复 |
| LongTerm | ChromaDB + SQLite | 永久 |
| MetaMemory | SQLite | 永久 |

> **多 Agent 隔离**：不同端口部署独立 daemon，独立 SQLite + PlanGraph checkpoint。详见 [agent-runloop.md](agent-runloop.md) §8。

---

## 五、完整交互示例

```
# 1. 连接 + 首条消息
C→S:  message(chat, seq:1, priority:1)
        {"role":"user","type":"message","subType":"chat","content":"搜 Transformer 论文",...}

# 2. 握手
S→C:  phase(connected, priority:0)
        {"role":"assistant","type":"phase","subType":"connected","sessionTitle":"...",...}

# 3. 思考 + 文本流（priority=0）
S→C:  thinking  →  {"role":"assistant","type":"thinking","delta":"分析意图...","done":false}
S→C:  message(text) → {"role":"assistant","type":"message","subType":"text","delta":"我来帮你","done":false}
S→C:  message(text) → {"role":"assistant","type":"message","subType":"text","delta":"","done":true}

# 4. 澄清（priority=2，阻塞）
S→C:  review(clarify) → {"role":"assistant","type":"review","subType":"clarify","questions":[...]}
C→S:  review(clarify) → {"role":"user","type":"review","subType":"clarify","answers":[...]}

# 5. 计划（priority=2，阻塞）
S→C:  review(plan) → {"role":"assistant","type":"review","subType":"plan","taskId":"t1","steps":[...]}
C→S:  review(plan) → {"role":"user","type":"review","subType":"plan","taskId":"t1","confirmed":true}

# 6. 执行（priority=0 展示 + priority=2 iOS工具）
S→C:  phase(execute, stageIndex:1, totalStages:6)
S→C:  tool(server) → {"role":"assistant","type":"tool","subType":"server","status":"running"}
S→C:  tool(server) → {"role":"assistant","type":"tool","subType":"server","status":"done"}
S→C:  tool(ios)    → {"role":"assistant","type":"tool","subType":"ios","id":"x1","name":"share_sheet"}
C→S:  tool(result) → {"role":"user","type":"tool","subType":"result","tool_call_id":"x1","content":"已分享"}
S→C:  phase(execute, stageIndex:3)

# 7. 用户取消任务（priority=2）
C→S:  review(task_control) → {"role":"user","type":"review","subType":"task_control","action":"cancel"}
S→C:  phase(paused)

# 8. 完成
S→C:  message(reply, priority:1)
        {"role":"assistant","type":"message","subType":"reply","content":"## 结果\n...","files":[...]}

# 9. 新任务创建 + 进度（🆕 v7.0）
S→C:  task(started) → {"role":"assistant","type":"task","subType":"started",
        "payload":{"taskId":"t2","name":"入库 Transformer","mode":"foreground","totalStages":7}}
S→C:  task(running) → {"role":"assistant","type":"task","subType":"running",
        "payload":{"taskId":"t2","stage":"搜索论文","stageIndex":1,"current":18,"total":50}}

# 10. 用户发新消息 → 前台任务自动转入后台（🆕 v7.0）
C→S:  message(chat) → {"role":"user","type":"message","subType":"chat",
        "payload":{"content":"顺便帮我查一下 attention 机制"}}
S→C:  task(backgrounded) → {"role":"assistant","type":"task","subType":"backgrounded",
        "payload":{"taskId":"t2","reason":"user_new_message"}}
S→C:  message(notification) → {"title":"转入后台","body":"入库任务已转入后台",
        "category":"task_backgrounded"}
S→C:  (处理新消息: thinking → message(text) → message(reply)...)

# 11. 后台任务完成（🆕 v7.0）
S→C:  task(running) → {"role":"assistant","type":"task","subType":"running",
        "payload":{"taskId":"t2","stage":"生成综述","stageIndex":7,"current":48,"total":50}}
S→C:  task(done) → {"role":"assistant","type":"task","subType":"done",
        "payload":{"taskId":"t2","result":{"total":50,"downloaded":48,"failed":2}}}
```

---

## 六、iOS 本地 Tool 配置

每次 `message(chat)` 携带可用工具列表。Server 通过 `tool(ios)` 请求执行。

### 工具定义格式

```json
{
  "name": "share_sheet",
  "description": "打开系统分享菜单",
  "parameters": {
    "type": "object",
    "properties": {
      "text": {"type": "string", "description": "分享文本"},
      "url": {"type": "string", "description": "分享链接"}
    },
    "required": []
  }
}
```

### 可用 Tool

| Tool | 说明 |
|------|------|
| `share_sheet` | 系统分享菜单 |
| `open_url` | Safari 打开 URL |
| `save_file` | 保存文件到本地 |
| `pick_file` | 从文件选择器选取文件 |
| `notification_permission` | 请求通知权限 |

---

> **版本**: v7.0 | 8 大类（type）+ 27 子类（subType）。新增 task 类型（5 子类）。priority: 0=流式 1=普通 2=阻塞。foreground→background 单向不可逆。APNs: 0不推 1合并 2立即 | 2026-06-16
> **配套文档**: [agent-runloop.md](agent-runloop.md) · [REST API](./rest-api.md)
