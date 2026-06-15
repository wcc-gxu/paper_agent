# Paper Agent v3 — WebSocket 协议规范

> iOS 客户端对接文档 | 2026-06-14

---

## 1. 连接

```
ws://{host}:{port}/ws/chat/{session_id}

session_id: 首次连接由客户端生成 UUID，后续重连使用相同 ID
```

---

## 2. Client → Server 消息

### 2.1 用户消息

每次发送都带上当前可用的 iOS 工具列表。

```json
{
  "type": "message",
  "content": "帮我搜索自动驾驶安全方向的论文",
  "ios_tools": [
    {
      "name": "share_sheet",
      "description": "打开系统分享菜单，分享文本/链接/文件",
      "parameters": {
        "type": "object",
        "properties": {
          "text": {"type": "string", "description": "要分享的文本"},
          "url": {"type": "string", "description": "要分享的链接"}
        },
        "required": []
      }
    },
    {
      "name": "open_url",
      "description": "用Safari打开URL",
      "parameters": {
        "type": "object",
        "properties": {
          "url": {"type": "string", "description": "要打开的URL"}
        },
        "required": ["url"]
      }
    }
  ]
}
```

### 2.2 工具执行结果

iOS 执行完 Agent 请求的工具后回传。

```json
{
  "type": "tool_result",
  "tool_call_id": "call_abc123",
  "content": "已成功分享到微信"
}
```

错误时：

```json
{
  "type": "tool_result",
  "tool_call_id": "call_abc123",
  "content": null,
  "error": "用户取消了分享操作"
}
```

### 2.3 控制指令

```json
{
  "type": "control",
  "task_id": "task_xyz",
  "action": "pause"
}
```

| action | 说明 |
|--------|------|
| `pause` | 暂停当前任务 |
| `resume` | 恢复已暂停的任务 |
| `cancel` | 取消任务 |

---

## 3. Server → Client 事件

### 3.1 text_delta — LLM 流式文字

```json
{
  "type": "text_delta",
  "text": "我来帮你搜索自动驾驶安全方向的论文。"
}
```

- 逐 token 或逐句推送
- iOS 端追加到对话 UI

### 3.2 tool_use — Agent 调用工具

服务端工具（iOS 仅展示）：

```json
{
  "type": "tool_use",
  "id": "call_search_1",
  "name": "search_papers",
  "input": {
    "keywords": "autonomous driving AND safety",
    "sources": "arxiv,semantic_scholar",
    "year_from": 2023,
    "max_results": 20
  },
  "location": "server",
  "timeout": null
}
```

iOS 工具（iOS 需要执行）：

```json
{
  "type": "tool_use",
  "id": "call_share_1",
  "name": "share_sheet",
  "input": {
    "text": "推荐论文: Attention Is All You Need",
    "url": "https://arxiv.org/abs/1706.03762"
  },
  "location": "ios",
  "timeout": 30
}
```

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识，iOS 回传 tool_result 时用 |
| `name` | 工具名称 |
| `input` | 工具参数 |
| `location` | `"server"` = iOS 只展示 / `"ios"` = iOS 需要执行 |
| `timeout` | 仅 `ios` 工具有效，超时秒数。由 LLM 设定 |

### 3.3 status — 实时进度

```json
{
  "type": "status",
  "phase": "execute",
  "step": {
    "current": 3,
    "total": 8
  },
  "message": "正在搜索 Semantic Scholar (3/8)..."
}
```

| phase | 说明 | iOS UI 建议 |
|-------|------|------------|
| `clarify` | 正在分析需求 | 展示思考动画 |
| `plan` | 方案已生成 | 展示方案卡片 |
| `execute` | 正在执行 | 展示进度条 + 当前步骤名 |
| `verify` | 正在验证 | 展示"评估中..." |
| `summarize` | 正在生成报告 | 展示生成动画 |
| `done` | 全部完成 | 展示结果摘要 |

### 3.4 message_stop — 最终回复

```json
{
  "type": "message_stop",
  "content": "## 研究方案\n\n我找到了 18 篇高相关论文...\n\n### 核心发现\n- ...",
  "plan": {
    "goal": "自动驾驶 AI 安全综述",
    "steps": [...]
  },
  "survey_path": "/root/papers/outputs/proj_abc/survey.md",
  "files": [
    {
      "name": "survey.md",
      "path": "/root/papers/outputs/proj_abc/survey.md",
      "size_bytes": 45200
    },
    {
      "name": "references.bib",
      "path": "/root/papers/outputs/proj_abc/references.bib",
      "size_bytes": 3200
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `content` | Markdown 格式的最终回复 |
| `plan` | 可选，如果有方案 |
| `survey_path` | 可选，综述文件路径 |
| `files` | 可选，可下载文件列表 |

---

## 4. 连接生命周期

```
iOS 连接 WS(/ws/chat/{session_id})
    │
    ▼
Server: 恢复会话状态 (从 LangGraph checkpoint)
    │
    ▼
Server: 回放断连期间的事件 (从 Redis 事件队列)
    │  ← 用户看到 "你离开期间，Agent 找到了5篇新论文..."
    │
    ▼
正常交互: user_message ↔ text_delta / tool_use / status / message_stop
    │
    ▼
iOS 断开:
    Agent 继续执行未完成的任务
    事件缓存到 Redis
    主动推送通过 APNs
    │
    ▼
iOS 重连:
    Server 恢复 → 回放事件 → 正常交互
```

---

## 5. 错误处理

```json
{
  "type": "error",
  "code": "TOOL_TIMEOUT",
  "message": "iOS 工具 'open_url' 在 30s 内未响应",
  "tool_call_id": "call_xyz",
  "recoverable": true
}
```

| code | 说明 |
|------|------|
| `TOOL_TIMEOUT` | iOS 工具超时未响应 |
| `TOOL_NOT_FOUND` | iOS 声明的工具不存在 |
| `SESSION_EXPIRED` | 会话 ID 无效 |
| `INTERNAL_ERROR` | 服务端内部错误 |

---

## 6. 完整交互示例

```
# 首次连接
iOS → Server:  WS /ws/chat/sess_abc 连接建立

# 发送消息
iOS → Server:
  {"type":"message","content":"搜自动驾驶安全论文,近3年","ios_tools":[...]}

# Agent 开始工作
Server → iOS:
  {"type":"text_delta","text":"我来帮你分析需求..."}
Server → iOS:
  {"type":"status","phase":"clarify","step":{"current":0,"total":0},"message":"分析需求..."}

# 工具调用
Server → iOS:
  {"type":"status","phase":"execute","step":{"current":1,"total":8},"message":"搜索论文中"}
Server → iOS:
  {"type":"tool_use","id":"c1","name":"search_papers","input":{...},"location":"server"}

# iOS 工具调用
Server → iOS:
  {"type":"tool_use","id":"c2","name":"share_sheet","input":{"text":"找到23篇论文"},"location":"ios","timeout":15}

iOS → Server:
  {"type":"tool_result","tool_call_id":"c2","content":"已分享"}

# 完成
Server → iOS:
  {"type":"message_stop","content":"## 综述\n...","files":[...]}
```

---

## 7. REST API 轮询进度（WS 推送的补充）

当 WebSocket 连接不可用（如 iOS 处于后台、网络切换），iOS 可通过 REST API 轮询任务进度。数据来源于子Agent 写入的 JSON 日志文件。

### 7.1 端点

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/projects/{id}/progress` | 返回项目最新 task 的进度摘要（当前阶段、阶段进度、最近 N 篇论文状态） |
| GET | `/api/tasks/{task_id}/log` | 返回指定 task 的 JSONL 日志内容（支持 `?tail=N` 获取最近 N 行） |
| GET | `/api/tasks/active` | 列出当前活跃（运行中）的 task 列表 |

### 7.2 `/api/projects/{id}/progress` 响应格式

```json
{
  "project_id": "proj-xxx",
  "task_id": "task-20260614-001",
  "status": "running",
  "current_stage": "download",
  "stage_index": 3,
  "total_stages": 7,
  "stages": [
    {"name": "search", "status": "done", "duration_ms": 44000, "result": {"total_found": 48}},
    {"name": "evaluate", "status": "done", "duration_ms": 34000, "result": {"relevant": 22}},
    {"name": "download", "status": "running", "current": 12, "total": 22},
    {"name": "convert", "status": "pending"},
    {"name": "index", "status": "pending"},
    {"name": "rank", "status": "pending"},
    {"name": "survey", "status": "pending"}
  ],
  "recent_papers": [
    {"paper_id": "paper-001", "title": "Attention Is All You Need", "download": "done"},
    {"paper_id": "paper-005", "title": "BERT: Pre-training...", "download": "failed", "error": "PDF not available"}
  ]
}
```

### 7.3 `/api/tasks/{task_id}/log?tail=N` 响应格式

返回 JSONL 原文的最近 N 行（默认 50），每行一个 JSON 事件对象。前端增量解析，与已有状态合并后更新 UI。

### 7.4 iOS 轮询策略

```
初次加载: GET /api/projects/{id}/progress → 渲染完整进度页
定时轮询: 每 3 秒 GET /api/tasks/{task_id}/log?tail=50 → 增量更新
完成检测: 读到 task_done 事件 → 停止轮询 → 展示最终摘要
错误处理: 3 次超时 → 切换到 10 秒间隔 → 恢复后回到 3 秒
```

### 7.5 与 WS `status` 消息的关系

| 场景 | 使用方式 |
|------|----------|
| iOS 前台 + WS 已连接 | WS `status` 实时推送为主，REST 不轮询 |
| iOS 后台 / WS 断开 | REST 轮询接管，WS 重连后切换回推送 |
| 首次打开进度页 | REST 拉取全量快照，WS 接管后续增量 |

---

> 版本: v1.1 | 新增 REST 轮询进度章节
