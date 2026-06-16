# Paper Agent v3 — WebSocket 通信协议

> iOS 客户端对接规范 | 2026-06-16

---

## 1. 连接

```
ws://{host}:{port}/ws/chat/{agent_id}/{session_id}

agent_id:  主 Agent 的唯一标识（如 "agent-001"）。一个用户 = 一个 Agent
session_id: 会话标识。一个 Agent 可以有多个 session（不同研究项目、不同设备）
```

### 1.1 默认值（MVP 写死）

| 值 | 说明 |
|----|------|
| `agent_id` = `"agent-001"` | 系统启动时自动创建，永久存在 |
| `session_id` = `"main"` | Agent 创建时附带，默认对话入口 |
| main session 标题 = `"我的小助理"` | 第一条消息后 LLM 自动更新 |

### 1.2 agent_id 与 session_id

| 概念 | 类比 | 生命周期 | 隔离内容 |
|------|------|----------|----------|
| `agent_id` | 你的"科研分身" | 永久 | 所有记忆、论文、偏好 |
| `session_id` | 你和 Agent 的一次"话题" | 按需创建/归档 | ShortTerm 上下文、MidTerm checkpoint |

```
agent-001 (一个用户，一个 Agent)
│
├── session: "main"           ← 默认会话 (bootstrap 创建，标题 "新对话")
│
├── session: "cv-project"     ← 用户创建的独立会话
│
└── session: "temp-abc"       ← 临时会话，关闭后不保留
```

### 1.3 连接握手（首条 chat 隐含握手）

**WS 连接建立后，iOS 立即发送第一条 `chat` 消息，不需要等 Server。** `connected` 是 Server 收到首条 chat 后的确认回复，不是前置条件。

```
iOS → Server:  WS 连接 /ws/chat/agent-001/main
iOS → Server:  chat({content: "搜 Transformer 论文", seq: 1})   ← 立即发送

Server:
  1. 收到 chat → 检查 session（不存在则自动创建）
  2. 发送 connected（确认 session 就绪 + 已有元数据）
  3. 处理 chat 消息 → LLM 响应
  4. 如果是 session 第一条消息 → LLM 生成标题 → session_updated
```

```json
// Server → iOS（收到第一条 chat 后的确认回复）
// 已有历史的 session:
{
  "role": "assistant", "type": "connected",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:30:00.100Z",
  "payload": {
    "status": "ok",
    "sessionTitle": "Transformer 文献调研",    // 历史 session 有标题
    "historyCount": 42,
    "activeTasks": [
      {"taskId": "task-001", "type": "ingest", "stage": "download", "progress": "12/22"}
    ]
  }
}

// 新 session（首条消息，标题尚未生成）:
{
  "role": "assistant", "type": "connected",
  "agentId": "agent-001", "sessionId": "session-new", "priority": 2,
  "timestamp": "2026-06-16T10:30:00.100Z",
  "payload": {
    "status": "ok",
    "sessionTitle": null,                     // ← 标题尚未生成
    "historyCount": 0,
    "activeTasks": []
  }
}
```

> `sessionTitle: null` 表示标题待生成。iOS 此时展示"新对话"占位。
> Server 处理完第一条 chat 后生成标题 → 推 `session_updated`。

### 1.4 Session 不存在时的自动创建

iOS 连接不存在的 session → 收到首条 chat 时 Server 自动创建，iOS 感知不到差异。

```
iOS → WS /ws/chat/agent-001/session-not-exist
iOS → chat({content: "你好", seq: 1})

Server:
  1. 收到 chat → 查 sessions 表 → 不存在
  2. 自动创建 session (title=null)
  3. 发送 connected({sessionTitle: null, historyCount: 0})
  4. 处理 chat → LLM 生成标题 "初次问候"
  5. 发送 text_delta × N
  6. 发送 session_updated({title: "初次问候"})
  7. 发送 message_stop
```

**两种获取 session 的方式并存：**

| 方式 | 适用场景 |
|------|----------|
| REST `GET /sessions` | iOS 启动时展示对话列表（离线也可看） |
| WS 直连自动创建 | 用户从通知点进来、深链接、刷新后快速恢复 |

### 1.5 Session 标题更新通知

LLM 生成标题后，Server 通过 WS 推送标题变更：

```json
{
  "role": "assistant",
  "type": "session_updated",
  "agentId": "agent-001",
  "sessionId": "main",
  "priority": 2,
  "timestamp": "2026-06-16T10:30:05Z",
  "payload": {
    "title": "Transformer文献调研"
  }
}
```

iOS 收到后更新对话列表中的标题。priority=2（不推送 APNs）。

### 1.6 当用户需要隔离上下文时

iOS 上点"新建对话" → POST `/api/agents/agent-001/sessions` → 获得 `session_id` → 连接 WS。用户也可以直接连接一个不存在的 session_id → Server 自动创建。

### 1.3 API — 获取 Agent 和 Session 列表

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agents` | 列出所有 Agent |
| GET | `/api/agents/{agent_id}` | Agent 详情（含 manifest 摘要） |
| GET | `/api/agents/{agent_id}/sessions` | 列出 Agent 的所有 session（含标题、最近消息时间） |
| POST | `/api/agents/{agent_id}/sessions` | 创建新 session → 返回 session_id |
| PATCH | `/api/agents/{agent_id}/sessions/{id}` | 更新 session（如修改标题） |
| DELETE | `/api/agents/{agent_id}/sessions/{id}` | 归档/删除 session |

### 1.4 Session 自动标题 — 参考腾讯元宝

腾讯元宝等 App 用第一条消息作为对话标题。服务端在收到 session 的第一条 chat 后，LLM 自动生成标题：

```
Server 内部:
  收到 session 第一条消息 →
  LLM 调用: "为以下对话生成一个 10 字以内的标题: {first_message}" →
  写入 sessions 表 title 字段 →
  iOS 通过 GET /sessions 获取标题列表
```

响应中附带 `title`：
```json
{
  "sessionId": "session-cv-project",
  "title": "YOLO目标检测调研",
  "createdAt": "2026-06-16T10:00:00Z",
  "lastMessageAt": "2026-06-16T15:30:00Z",
  "messageCount": 42
}
```

### 1.5 心跳

| 方向 | 消息 | 间隔 |
|------|------|------|
| iOS → Server | `{"type":"ping"}` | 每 30 秒 |
| Server → iOS | `{"type":"pong"}` | 收到 ping 立即回复 |

客户端 90 秒内未收到任何消息（包括 pong）→ 断开重连。

---

## 2. 消息格式

### 通用信封

```json
{
  "role": "user | assistant",
  "type": "<msgType>",
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
| `role` | string | `user` = 客户端发出，`assistant` = 服务端发出 |
| `type` | string | 消息类型（见 §3） |
| `agentId` | string | Agent ID（如 `agent-001`）。一个用户 = 一个 Agent |
| `sessionId` | string | 会话 ID。同一 Agent 可以有多个 session（隔离上下文） |
| `seq` | int | 消息序列号（仅客户端消息需要）。Server 收到新 `seq` 且大于当前处理中的 `seq` → 放弃当前处理 |
| `priority` | int | `0`=critical `1`=normal `2`=low |
| `timestamp` | string | ISO 8601 UTC |
| `payload` | object | 消息体，按 type 不同 |

### priority 规则

| priority | 含义 | 触发 APNs | 示例 |
|----------|------|-----------|------|
| `0` critical | 需要用户立即关注 | ✅ 是 | plan 待确认、任务失败、错误 |
| `1` normal | 有意义的业务消息 | ✅ 是（合并推送）| 阶段完成、tool 结果、完整回复 |
| `2` low | 流式/心跳/展示 | ❌ 否 | text_delta、thinking_delta、tool_use 展示、ping/pong |

---

## 3. 消息类型总览

### 3.0 连接与会话管理

| type | role | priority | 说明 |
|------|------|----------|------|
| `connected` | assistant | 2 | WS 连接建立后 Server 发的第一条消息，含 session 元数据 |
| `session_updated` | assistant | 2 | session 标题变更 |
| `ping` | user | 2 | 客户端心跳 |
| `pong` | assistant | 2 | 服务端心跳回复 |

#### connected（Server → iOS，握手）

```json
{
  "role": "assistant", "type": "connected",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "timestamp": "2026-06-16T10:30:00Z",
  "payload": {
    "agentName": "我的科研助理",
    "sessionTitle": "Transformer 文献调研",
    "sessionCreatedAt": "2026-06-16T08:00:00Z",
    "historyCount": 42,
    "activeTasks": []
  }
}
```

> iOS 发第一条 chat 后，Server 回复 `connected` 确认 session 就绪。`sessionTitle: null` 表示标题待 LLM 生成。

#### session_updated（Server → iOS，标题变更）

```json
{
  "role": "assistant", "type": "session_updated",
  "agentId": "agent-001", "sessionId": "main", "priority": 2,
  "payload": {"title": "Transformer文献调研"}
}
```

```json
// iOS → Server
{"role":"user","type":"ping","sessionId":"...","priority":2,"timestamp":"...","payload":{}}

// Server → iOS
{"role":"assistant","type":"pong","sessionId":"...","priority":2,"timestamp":"...","payload":{}}
```

---

### 3.1 对话消息

| type | role | priority | 说明 |
|------|------|----------|------|
| `chat` | user | 1 | 用户文本消息 |
| `text_delta` | assistant | 2 | LLM 流式文本（逐 token 或全文。done=true 表示文本块结束） |
| `thinking_delta` | assistant | 2 | LLM 流式思考，逐 token |
| `message_stop` | assistant | 1 | 本轮 LLM 回复结束（含完整文本和附件） |

#### chat（iOS → Server）

```json
{
  "role": "user",
  "type": "chat",
  "agentId": "agent-001",
  "sessionId": "main",
  "seq": 1,
  "priority": 1,
  "timestamp": "2026-06-16T10:30:00Z",
  "payload": {
    "content": "帮我搜索 Transformer 注意力机制的论文",
    "ios_tools": [
      {
        "name": "share_sheet",
        "description": "打开系统分享菜单",
        "parameters": {
          "type": "object",
          "properties": {
            "text": {"type": "string"},
            "url": {"type": "string"}
          },
          "required": []
        }
      }
    ]
  }
}
```

> `seq`：每次 chat 消息递增。Server 收到新 chat 且 `seq` > 当前处理中的 `seq` → 放弃当前 clarify/plan/execute 流程，开始新消息。
> `ios_tools`：iOS 端当前可用的工具列表。每次 chat 消息都带上。
> `sessionId`：指定对话上下文。不同 session 的 ShortTerm 互不干扰。

#### text_delta（Server → iOS，流式/全文）

**流式模式**（逐 token）：
```json
{
  "role": "assistant", "type": "text_delta",
  "sessionId": "sess-abc123", "priority": 2,
  "timestamp": "2026-06-16T10:30:01.500Z",
  "payload": {"index": 0, "delta": "我来", "done": false}
}
```

**流式结束**（最后一条）：
```json
{
  "role": "assistant", "type": "text_delta",
  "sessionId": "sess-abc123", "priority": 2,
  "payload": {"index": 0, "delta": "", "done": true}
}
```

**非流式模式**（全文一次返回）：
```json
{
  "role": "assistant", "type": "text_delta",
  "sessionId": "sess-abc123", "priority": 2,
  "payload": {"index": 0, "delta": "完整回复文本...", "done": true}
}
```

| 字段 | 说明 |
|------|------|
| `index` | LLM 回复序号（每轮对话可能有多次 tool_use 交替，index 递增） |
| `delta` | 流式时=当前 token，非流式时=完整文本 |
| `done` | `true` 表示当前 text block 结束 |

> **注意**：没有单独的 `text_done` 类型。`text_delta(done=true)` 统一表示文本块结束。
> **流式中断处理**：iOS 重连时，未完成的 `text_delta` 流被丢弃。Server 收到重连信号后等待 LLM 完成 → 直接发 `message_stop` 带完整文本。iOS 重连后丢弃未完成流，展示 `message_stop`。

#### thinking_delta（Server → iOS，流式思考）

```json
{
  "role": "assistant",
  "type": "thinking_delta",
  "sessionId": "sess-abc123",
  "priority": 2,
  "timestamp": "2026-06-16T10:30:01.200Z",
  "payload": {
    "delta": "我需要先分析用户的意图...",
    "done": false
  }
}
```

> iOS 展示建议：折叠在"思考中..."区域内，默认折叠，用户可展开查看。

#### message_stop（Server → iOS）

```json
{
  "role": "assistant",
  "type": "message_stop",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:35:00Z",
  "payload": {
    "content": "## 调研结果\n\n找到 48 篇论文，22 篇高相关...",
    "files": [
      {"name": "survey.md", "path": "/papers/outputs/proj-xxx/survey.md", "size_bytes": 45200}
    ]
  }
}
```

> LLM 本轮完整回复结束。可能包含文件附件。

---

### 3.2 工具调用

| type | role | priority | 说明 |
|------|------|----------|------|
| `tool_use` | assistant | 1 | Server 请求 iOS 执行工具 |
| `tool_use_display` | assistant | 2 | Server 工具执行中（iOS 仅展示） |
| `tool_result` | user | 1 | iOS 回传工具执行结果 |

#### tool_use（Server → iOS，需要 iOS 执行）

```json
{
  "role": "assistant",
  "type": "tool_use",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:30:03Z",
  "payload": {
    "id": "call_abc123",
    "name": "share_sheet",
    "input": {
      "text": "推荐论文: Attention Is All You Need",
      "url": "https://arxiv.org/abs/1706.03762"
    },
    "location": "ios",
    "timeout": 30
  }
}
```

| 字段 | 说明 |
|------|------|
| `id` | 工具调用唯一 ID，iOS 回传 `tool_result` 时用 |
| `name` | 工具名称 |
| `input` | 工具参数 |
| `location` | `"ios"` = iOS 必须执行并回传结果 |
| `timeout` | 超时秒数，仅 `location: "ios"` 时有效 |

#### tool_use_display（Server → iOS，仅展示）

```json
{
  "role": "assistant",
  "type": "tool_use_display",
  "sessionId": "sess-abc123",
  "priority": 2,
  "timestamp": "2026-06-16T10:30:03Z",
  "payload": {
    "name": "search_papers",
    "input": {
      "keywords": "transformer attention mechanism",
      "sources": "arxiv,semantic_scholar",
      "max_results": 20
    },
    "location": "server",
    "status": "running"
  }
}
```

| 字段 | 说明 |
|------|------|
| `location` | `"server"` = iOS 仅展示，不执行 |
| `status` | `"running"` = 执行中 / `"done"` = 完成 / `"failed"` = 失败 |

> 同一个 server tool 调用会收到多条 `tool_use_display`：status 从 running → done/failed。iOS 可据此显示 spinner → ✓/✗。

#### tool_result（iOS → Server）

```json
{
  "role": "user",
  "type": "tool_result",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:30:05Z",
  "payload": {
    "tool_call_id": "call_abc123",
    "content": "已成功分享到微信"
  }
}
```

错误时：
```json
{
  "role": "user",
  "type": "tool_result",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:30:05Z",
  "payload": {
    "tool_call_id": "call_abc123",
    "content": null,
    "error": "用户取消了分享操作"
  }
}
```

---

### 3.3 Plan 流程

| type | role | priority | 说明 |
|------|------|----------|------|
| `clarify_question` | assistant | 1 | 澄清问题 |
| `clarify_response` | user | 1 | 用户回答澄清问题 |
| `plan` | assistant | 1 | 生成的执行计划 |
| `plan_confirm` | user | 0 | 用户确认/拒绝计划 |
| `plan_rejected` | assistant | 0 | 计划被拒绝的响应 |

#### clarify_question（Server → iOS）

```json
{
  "role": "assistant",
  "type": "clarify_question",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:30:01Z",
  "payload": {
    "message": "为了更准确地搜索，请确认以下问题：",
    "questions": [
      {
        "id": "q1",
        "question": "您关注的是 AI 安全(security)还是功能安全(safety)？",
        "options": ["AI 安全 (security)", "功能安全 (safety)", "两者都关注"]
      },
      {
        "id": "q2",
        "question": "时间范围？",
        "options": ["近 1 年", "近 3 年", "近 5 年", "不限"]
      }
    ]
  }
}
```

#### clarify_response（iOS → Server）

```json
{
  "role": "user",
  "type": "clarify_response",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:30:10Z",
  "payload": {
    "original_query": "帮我搜索 Transformer 注意力机制",
    "answers": [
      {"question_id": "q1", "answer": "AI 安全 (security)"},
      {"question_id": "q2", "answer": "近 3 年"}
    ]
  }
}
```

#### plan（Server → iOS）

```json
{
  "role": "assistant",
  "type": "plan",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:30:15Z",
  "payload": {
    "taskId": "task-20260616-001",
    "goal": "Transformer 注意力机制 AI 安全方向文献调研",
    "summary": "分 3 次搜索，预计找到 40-60 篇论文",
    "steps": [
      {"index": 1, "action": "search", "description": "搜索 adversarial attack on attention", "source": "semantic_scholar", "max_papers": 30},
      {"index": 2, "action": "search", "description": "搜索 transformer robustness verification", "source": "arxiv", "max_papers": 20},
      {"index": 3, "action": "evaluate", "description": "LLM 评估相关性，筛选高相关论文"},
      {"index": 4, "action": "download", "description": "下载高相关论文 PDF"},
      {"index": 5, "action": "convert_index", "description": "转换 Markdown 并索引入库"},
      {"index": 6, "action": "survey", "description": "生成文献综述报告"}
    ],
    "markdown": "## 研究方案\n\n### 目标\nTransformer 注意力机制在 AI 安全方向...\n\n### 执行步骤\n1. ..."
  }
}
```

#### plan_confirm（iOS → Server）

```json
// 确认
{
  "role": "user",
  "type": "plan_confirm",
  "sessionId": "sess-abc123",
  "priority": 0,
  "timestamp": "2026-06-16T10:30:20Z",
  "payload": {
    "taskId": "task-20260616-001",
    "confirmed": true,
    "modifications": null
  }
}

// 拒绝
{
  "role": "user",
  "type": "plan_confirm",
  "sessionId": "sess-abc123",
  "priority": 0,
  "timestamp": "2026-06-16T10:30:20Z",
  "payload": {
    "taskId": "task-20260616-001",
    "confirmed": false,
    "reason": "搜索范围太窄了"
  }
}

// 修改后确认
{
  "role": "user",
  "type": "plan_confirm",
  "sessionId": "sess-abc123",
  "priority": 0,
  "timestamp": "2026-06-16T10:30:20Z",
  "payload": {
    "taskId": "task-20260616-001",
    "confirmed": true,
    "modifications": {
      "steps": [
        {"index": 1, "max_papers": 50}
      ]
    }
  }
}
```

---

### 3.4 进度与状态

| type | role | priority | 说明 |
|------|------|----------|------|
| `status` | assistant | 1 | 任务进度更新 |
| `notification` | assistant | 0/1 | 通知（含 APNs 推送） |

#### status（Server → iOS）

```json
{
  "role": "assistant",
  "type": "status",
  "sessionId": "sess-abc123",
  "priority": 1,
  "timestamp": "2026-06-16T10:31:00Z",
  "payload": {
    "taskId": "task-20260616-001",
    "phase": "execute",
    "stage": "搜索论文",
    "stageIndex": 1,
    "totalStages": 6,
    "message": "正在搜索 Semantic Scholar (第 1/3 轮)...",
    "detail": {
      "papersFound": 18,
      "papersTotal": 30,
      "currentPaper": "Attention Is All You Need"
    }
  }
}
```

| phase | 触发时机 | iOS UI 建议 |
|-------|---------|------------|
| `preparing` | Plan 确认后，子 Agent 初始化中 | 展示"准备中..."spinner |
| `clarify` | 正在分析需求 | 展示思考动画 |
| `plan` | 方案已生成 | 展示方案卡片 |
| `execute` | 正在执行 | 展示进度条 + 当前步骤 |
| `verify` | 正在验证结果 | 展示"验证中..." |
| `summarize` | 正在生成报告 | 展示生成动画 |
| `paused` | 任务已暂停 | 展示暂停状态 + 继续按钮 |
| `recovering` | 系统重启恢复中 | 展示"恢复中..."spinner |
| `done` | 全部完成 | 展示结果摘要 |

#### notification（Server → iOS）

```json
{
  "role": "assistant",
  "type": "notification",
  "sessionId": "sess-abc123",
  "priority": 0,
  "timestamp": "2026-06-16T12:00:00Z",
  "payload": {
    "title": "入库完成",
    "body": "Transformer 文献调研完成：48 篇论文，22 篇高相关已入库",
    "category": "task_complete",
    "data": {
      "taskId": "task-20260616-001",
      "projectId": "proj-xxx"
    }
  }
}
```

> `priority: 0` → 触发 APNs。`priority: 1` → 合并推送（多条合并为一条"有 N 条新消息"）。`priority: 2` → 不推送。

---

### 3.5 控制指令

| type | role | priority | 说明 |
|------|------|----------|------|
| `task_control` | user | 0 | 暂停/恢复/取消任务 |
| `task_control_ack` | assistant | 0 | 控制指令确认 |

#### task_control（iOS → Server）

```json
{
  "role": "user",
  "type": "task_control",
  "sessionId": "sess-abc123",
  "priority": 0,
  "timestamp": "2026-06-16T10:35:00Z",
  "payload": {
    "taskId": "task-20260616-001",
    "action": "pause"
  }
}
```

| action | 说明 |
|--------|------|
| `pause` | 暂停任务（当前阶段完成后暂停） |
| `resume` | 恢复已暂停的任务 |
| `cancel` | 取消任务 |

#### task_control_ack（Server → iOS）

```json
{
  "role": "assistant",
  "type": "task_control_ack",
  "sessionId": "sess-abc123",
  "priority": 0,
  "timestamp": "2026-06-16T10:35:01Z",
  "payload": {
    "taskId": "task-20260616-001",
    "action": "pause",
    "status": "ok",
    "message": "任务将在当前阶段完成后暂停"
  }
}
```

---

### 3.6 错误

| type | role | priority | 说明 |
|------|------|----------|------|
| `error` | assistant | 0/1 | 错误通知 |

```json
{
  "role": "assistant",
  "type": "error",
  "sessionId": "sess-abc123",
  "priority": 0,
  "timestamp": "2026-06-16T10:32:00Z",
  "payload": {
    "code": "TOOL_TIMEOUT",
    "message": "iOS 工具 'share_sheet' 在 30s 内未响应",
    "toolCallId": "call_abc123",
    "recoverable": true
  }
}
```

| code | 说明 | recoverable |
|------|------|-------------|
| `TOOL_TIMEOUT` | iOS 工具超时未响应 | true |
| `TOOL_EXPIRED` | tool_result 迟于超时到达，已被忽略 | true |
| `TOOL_NOT_FOUND` | iOS 声明的工具不存在 | true |
| `SESSION_EXPIRED` | 会话 ID 无效 | false (需重连) |
| `TASK_FAILED` | 任务执行失败 | true |
| `INTERNAL_ERROR` | 服务端内部错误 | false |
| `AGENT_NOT_FOUND` | agent_id 不存在 | false |
| `RATE_LIMITED` | 请求速率限制 | true |

---

## 4. Session 内存隔离

| 记忆层 | main session | 命名 session | temp session |
|--------|-------------|-------------|-------------|
| ShortTerm | ✅ 独立窗口，重启恢复 | ✅ 独立窗口 | ✅ 当前窗口，断开丢弃 |
| MidTerm (checkpoint) | ✅ LangGraph 自动 | ✅ 可选 | ❌ |
| LongTerm (对话摘要) | ✅ 自动写入 agent_conversations | ⚠️ 用户手动 promote | ❌ |
| LongTerm (论文/RAD) | ✅ 全局共享 | ✅ 全局共享 | ✅ 全局共享 |
| MetaMemory (偏好) | ✅ 学习 | ❌ | ❌ |
| 术语库 | ✅ 全局共享 | ✅ 全局共享 | ✅ 全局共享 |

**规则**：main session 是持久入口；命名 session 默认不写 LongTerm（避免随口问题变永久记忆），用户说"记住"才 promote；temp session 断开即丢弃。RAD 层不做 session 隔离——所有 session 搜论文结果一致。

---

## 5. 完整交互示例

```
# 1. 连接 + 首条消息（隐含握手）
iOS → Server:  WebSocket /ws/chat/agent-001/main 连接建立
iOS → Server:  chat({content:"搜 Transformer 注意力机制论文", seq:1})
                ← 立即发送，不等 Server

# 2. Server 确认
Server → iOS:  connected({sessionTitle:"Transformer文献调研", historyCount:42})

# 3. Agent 开始回复
Server → iOS:
  {"role":"assistant","type":"thinking_delta","priority":2,"payload":{"delta":"用户想搜索论文...","done":false}}
Server → iOS:
  {"role":"assistant","type":"thinking_delta","priority":2,"payload":{"delta":"需要先澄清搜索范围","done":true}}
Server → iOS:
  {"role":"assistant","type":"text_delta","priority":2,"payload":{"delta":"我来帮你","done":false}}
Server → iOS:
  {"role":"assistant","type":"text_delta","priority":2,"payload":{"delta":"明确一下需求","done":true}}

# 4. 澄清问题
Server → iOS:
  {"role":"assistant","type":"clarify_question","priority":1,"payload":{"questions":[{"id":"q1","question":"..."}]}}

# 5. 用户回答
iOS → Server:
  {"role":"user","type":"clarify_response","priority":1,"payload":{"answers":[{"question_id":"q1","answer":"..."}]}}

# 6. 生成计划
Server → iOS:
  {"role":"assistant","type":"plan","priority":1,"payload":{"taskId":"task-001","steps":[...]}}

# 7. 用户确认
iOS → Server:
  {"role":"user","type":"plan_confirm","priority":0,"payload":{"taskId":"task-001","confirmed":true}}

# 8. 执行中 — Server 工具展示
Server → iOS:
  {"role":"assistant","type":"status","priority":1,"payload":{"phase":"execute","stage":"搜索","stageIndex":1,"totalStages":6}}
Server → iOS:
  {"role":"assistant","type":"tool_use_display","priority":2,"payload":{"name":"search_papers","location":"server","status":"running"}}
Server → iOS:
  {"role":"assistant","type":"tool_use_display","priority":2,"payload":{"name":"search_papers","location":"server","status":"done"}}
Server → iOS:
  {"role":"assistant","type":"status","priority":1,"payload":{"phase":"execute","stage":"下载","stageIndex":3,"totalStages":6}}

# 9. 可能穿插 iOS 工具调用
Server → iOS:
  {"role":"assistant","type":"tool_use","priority":1,"payload":{"id":"call_x1","name":"share_sheet","location":"ios","timeout":30,...}}
iOS → Server:
  {"role":"user","type":"tool_result","priority":1,"payload":{"tool_call_id":"call_x1","content":"已分享"}}

# 10. 完成
Server → iOS:
  {"role":"assistant","type":"message_stop","priority":1,"payload":{"content":"## 调研结果\n...","files":[...]}}
```

---

## 6. 连接生命周期

```
iOS 连接 WS(/ws/chat/{agent_id}/{session_id})
    │
    ▼
Server: 检查 agent_id + session_id 有效性
    │  ├── agent 不存在 → error(AGENT_NOT_FOUND)
    │  ├── session 无效 → error(SESSION_EXPIRED) → iOS 重新创建 session
    │  └── 有效 → 从 LangGraph checkpoint 恢复该 session 的状态
    │
    ▼
Server: 检查连接状态
    │  ├── 有未完成任务 → status(phase:"recovering") → 恢复进度
    │  ├── 有已完成任务 → notification(最近1条) + 聚合通知
    │  └── 有未完成流式 → 丢弃 delta 缓存 → 等 LLM 完成 → message_stop
    │
    ▼
正常交互: chat ↔ text_delta / thinking_delta / tool_use / status / message_stop
    │
    ▼
iOS 断开 (进入后台 / 网络切换 / 主动关闭):
    Server 标记连接状态 = disconnected
    Agent 继续执行未完成任务
    事件按 priority 分流:
      priority 0 → APNs 推送
      priority 1 → APNs 合并推送 + Redis 缓存
      priority 2 → 丢弃（不缓存，不推送）
    │
    ▼
iOS 重连:
    Server 标记连接状态 = connected
    停止 APNs 推送（用户已在线）
    Redis 回放 priority 1 缓存事件
    创建 HistoryAgent 处理过期消息
    正常交互
```

### 5.1 连接状态

Server 内部维护每条 WebSocket 连接的状态：

| 状态 | 含义 | WS 消息 | APNs |
|------|------|---------|------|
| `connected` | 正常连接 | ✅ 所有 priority | ❌ |
| `disconnected` | 已断开 | ❌ | priority 0/1 |
| `reconnecting` | 正在重连恢复 | status(phase:"recovering") | ❌ |

### 5.2 重连时的事件处理

| 事件类型 | 处理方式 |
|----------|----------|
| 未完成的 `text_delta` 流 | 丢弃。LLM 完成后直接发 `message_stop` |
| 未完成的 `thinking_delta` 流 | 丢弃。不缓存、不回放 |
| `tool_use_display` (server) | 不回放。重连后发一条 `status` 带当前阶段 |
| `notification` (已完成任务) | 只回放最近 1 条 `message_stop`，更早的合并为 "你离开期间完成了 N 个任务" |
| `clarify_question` | 如果未过期（< 30 分钟），重新发送。否则忽略 |
| `plan` | 如果未过期（< 30 分钟），重新发送。否则忽略 |
| `error` | 全部回放（用户需要知道出了什么错） |

### 5.3 后台切换与 APNs 降级

```
iOS 进入后台 (app 挂起):
  1. iOS 有 ~30 秒可以继续收 WS 消息
  2. 30 秒后系统断开 WS 连接
  3. Server 检测到 disconnect → 切换为 APNs 模式

iOS 进入前台 (app 激活):
  1. iOS 重新建立 WS 连接 (相同 sessionId)
  2. Server 检测到 reconnect → 停止 APNs → 走 5.2 重连流程

APNs 推送规则:
  - priority 0: 每条独立推送 (如 "任务失败"、"Plan 待确认")
  - priority 1: 合并推送 (最多每 10 分钟一条，如 "有 5 条新消息")
  - priority 2: 不推送
```

### 5.4 消息序列号 (seq) 与取消

```
iOS 发送消息时必须带递增的 `seq`:

iOS → chat(seq=1, "搜 Transformer")
iOS → chat(seq=2, "不对，搜 BERT")            ← seq=2 > 1
iOS → chat(seq=3, "算了看看库里有什么")       ← seq=3 > 2

Server:
  收到 seq=1 → 开始处理 (parse_intent)
  收到 seq=2 → seq > current_seq → 放弃 seq=1 的处理 → 开始 seq=2
  收到 seq=3 → seq > current_seq → 放弃 seq=2 的处理 → 开始 seq=3

Server 只处理最新的 seq 消息。之前的处理被取消（checkpoint 不回滚）。
```

### 5.5 进程崩溃恢复

```
[Server 崩溃]
  1. Agent 进程终止
  2. Redis AOF 保证崩溃前 1 秒内的事件已持久化
  3. LangGraph checkpoint (SQLite) 保证最后完成节点的状态已保存

[Server 重启]
  1. LangGraph 从 SQLite checkpoint 恢复 Plan Graph 状态
  2. Redis AOF 自动恢复事件队列
  3. Celery Worker 重启，未完成的任务重新入队

[iOS 重连]
  1. Server → status(phase:"recovering", message:"系统正在恢复...")
  2. 从 checkpoint 恢复 → 检查与 iOS 最后状态的差异
  3. Server → status(phase:"execute", message:"重新开始阶段 3...")
  4. 继续执行

数据丢失窗口: 崩溃前最后 1 秒 (AOF appendfsync everysec)。
            上一个 checkpoint 之后完成但未 checkpoint 的节点（通常 1 个节点）。
```

---

## 7. iOS 本地 Tool 配置

每次 `chat` 消息携带当前可用的 iOS 工具列表：

```json
{
  "name": "share_sheet",
  "description": "打开系统分享菜单，分享文本/链接/文件",
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

| iOS Tool | 说明 |
|----------|------|
| `share_sheet` | 系统分享菜单 |
| `open_url` | Safari 打开 URL |
| `save_file` | 保存文件到本地 |
| `pick_file` | 从文件选择器选取文件（如上传 PDF） |
| `notification_permission` | 请求通知权限 |

---

> 版本: v2.4 | 握手修正：首条chat隐含握手, connected变确认为回复, sessionTitle可为null | 2026-06-16
