# Paper Agent v5 — WebSocket 通信协议

> iOS / Vue 客户端对接规范 | v11.2 | 2026-07-20
>
> **v11.2 变更 (v5 架构)**:
> - `role` 字段从信封永久移除（v10 标记删除，v5 正式执行）
> - `plan_review`、`plan_todo_update`、`gate` 消息类型删除（v5 去规划节点，无 plan/gate）
> - `plan_approve`、`plan_revise` 入站消息删除（无计划审批）
> - `status` stage 值更新：去掉 `planning`/`verifying`，增加 `executing`/`responding`
> - 架构不感知 `tool_execution`（保留但 handler 节点不再使用）
>
> v11.1 新增: AGENT_NOT_RUNNING 错误状态、7 种 agent state 反馈
> v11.0 新增: AGENT_NOT_RUNNING error、queued status、doc_* 文档编辑工具
>
> 详见 [vue 客户端 WS 协议](../../paper-agent-vue/docs/websocket-protocol.md)

---

## 设计哲学

v10 围绕**小屏 iOS** 与 **Web SPA** 的交互范式重构,核心三条:

1. **内部编排对用户不可见**。主 Agent 的 intent_classify 等 LLM 调用是后端编排细节,用户不该看到。v5 从 plan→execute→evaluate 循环简化为 intent→handler 直连路由。需要让用户感知进度时,走人类可读的 `status` 消息("正在分析意图..."/"正在搜索知识库...")。
2. **用户操作入口唯一**。任何需要用户授权或澄清的交互——确认、单选、多选、自由文本、方案审批——**统一**走 `ask` 消息(客户端必须实现的唯一交互 tool)。其他 tool 调用(子 Agent / iOS 端 tool)只通过 `tool/*` 消息汇报进度,不渲染按钮。
3. **立即反馈,无空白**。用户发消息后服务端 <200ms 回 `status{stage:"received"}`,再开始 intent_classify(~5–10s)。期间持续推 `status` 阶段更新,用户绝不面对空白屏幕。

**协议规模**: outbound **10** 个 type + inbound **7** 个 type = 17 种消息。

---

## 一、总纲

### 1.1 WebSocket 地址

```
ws://{host}/ws/chat/{agent_id}/{session_id}?token=<jwt>
```

| 参数 | 说明 |
|------|------|
| `agent_id` | Agent 实例 ID。格式 `agent-{user_id}`,默认 `"agent-001"` |
| `session_id` | 会话 ID。默认 `"main"` |
| `token` | **JWT access_token** (v10.1 新增)。通过 `POST /api/auth/login` 获取。`JWT_SECRET` 未配置时可选 |

### 1.2 认证 (v10.1)

**JWT 认证流程**:

```
1. POST /api/auth/register 或 POST /api/auth/login → 获取 access_token + refresh_token
2. WebSocket 连接: ws://host/ws/chat/{agent_id}/{session_id}?token=<access_token>
3. 服务端验证 JWT → 提取 user_id → 交叉验证 agent_id 格式匹配
4. token 无效/过期 → 拒绝连接 (code 4001)
5. agent_id 不匹配 → 拒绝连接 (code 4003)
```

**token 刷新**: 客户端在 access_token 过期前调用 `POST /api/auth/refresh` 获取新 token，然后重新连接 WebSocket。

**开放模式**: 若 `JWT_SECRET` 未配置，`?token=` 参数可选，服务端从 `agent_id` 提取 `user_id`（开发/测试用）。

### 1.3 连接建立

客户端连接后发送 JWT token 作为查询参数。服务端验证通过后 accept 连接。之后立即发 ping,服务端回 pong,开始通信。服务端永不主动断开。

```
→ {"type":"ping","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{}}
← {"type":"pong","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{}}
```

### 1.4 离线消息拉取

客户端重连后通过 **REST API** 拉取离线消息（不再通过 WebSocket sync）：

```
GET /api/sessions/{session_id}/messages?since=<ISO_8601_timestamp>
→ {"messages": [...], "has_more": false}
```

返回的消息已去重（同一 tool_call_id 只保留最新状态），silent 消息（status/thinking）已排除。

### 1.5 通用信封

```json
{
  "type": "status | message | tool | tool_execution | plan_todo_update | ask | error | pong",
  "subType": "<子类,仅 tool/error 用>",
  "msg_id": "<uuid,server 出站必填>",
  "agentId": "agent-001",
  "sessionId": "main",
  "timestamp": "2026-07-13T12:00:00Z",
  "priority": "silent | normal | high | urgent",
  "capabilities": ["calendar", "location", "file_read"],
  "payload": {}
}
```

| 字段 | 方向 | 说明 |
|------|:----:|------|
| `type` | 双向 | 消息大类 |
| `subType` | 双向 | 子类,仅 `tool`/`error` 用 |
| `msg_id` | 出站 | UUID。客户端用于排重 / REST API 分页游标 |
| `agentId` / `sessionId` | 双向 | 路由标识 |
| `timestamp` | 双向 | ISO 8601 UTC |
| `priority` | 出站 | `silent`=不持久化 / `normal`=普通进度 / `high`=回复/审批/完成 → 可触发通知 / `urgent`=错误 |
| `capabilities` | **入站** | 客户端可用能力列表,**每条 inbound 消息都带**;服务端缓存最新值 |
| `payload` | 双向 | 消息体 |

> **v11.1→v11.2 变更**: `role` 正式删除; `plan_review`/`plan_todo_update`/`gate` 删除(v5 去规划节点); `plan_approve`/`plan_revise` 入站删除; 新增 `cleanup` stage 值。
> **v10→v10.1 变更**: `priorityKind` → `priority`; 新增 `plan_todo_update`/`tool_execution` 消息类型; WebSocket 连接新增 JWT 认证。

### 1.6 消息速查全表

**Outbound (server → client)**

| type | subType | priority | 说明 |
|------|---------|:--------:|------|
| `status` | — | normal | 人类可读阶段更新 |
| `message` | `reply` | high | LLM 最终 Markdown 回复 |
| `tool` | `start` | high | 启动子 Agent / 长任务 |
| `tool` | `progress` | normal | 任务进度更新 |
| `tool` | `result` | high | 任务终态(done/failed) |
| `tool` | `call` | high | 请求客户端执行本地 tool |
| `tool_execution` | — | normal | 每次 tool 调用的独立追踪消息 (v10.1) |
| `ask` | — | high | **唯一**用户操作入口(5 种 kind) |
| `error` | `TASK_FAILED` / `INTERNAL_ERROR` / `ASK_TIMEOUT` / `AGENT_NOT_RUNNING` | urgent | 错误 |
| `pong` | — | silent | 心跳回复 |

**Inbound (client → server)**

| type | 说明 |
|------|------|
| `ping` | 心跳 |
| `message` | 用户文本输入 |
| `ask_reply` | **统一**所有 Ask Card 回执 |
| `tool_result` | 仅 `tool/call` 的执行结果 |

---

## 二、Outbound 消息详述

### 2.1 `status` — 阶段更新(v10 新增)

人类可读的当前阶段反馈。**替代 v9 的 `message/thinking`**——内部 LLM 编排(intent/plan/eval 的 JSON 来回)**不产生任何用户可见消息**,只在节点入口/出口推一条 `status` 告诉用户"我在干什么"。

```json
{
  "type": "status",
  "msg_id": "...",
  "agentId": "agent-001",
  "sessionId": "main",
  "timestamp": "...",
  "priority": "normal",
  "payload": {
    "stage": "received",
    "message": "收到,正在分析...",
    "level": "user"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `stage` | string | **自由字符串**(非强制枚举)。推荐值见 §5.1 |
| `message` | string | 用户可见中文(≤60 字) |
| `level` | `"user" \| "debug"` | `user`=正常显示;`debug`=仅 `DEBUG_PROTOCOL=1` + iOS dev build 渲染。详见 §5.2 |

**关键约束**:同一 turn 内,新的 `status` 替换旧的(视觉上是同一条气泡在更新文本);但历史里保留每条(回滚可查)。iOS 渲染成 typing 指示器风格。

### 2.2 `message/reply` — 最终回复

LLM 完整 Markdown 回复。**一次性发送,不流式 token**(长综述生成期间由 `status` 撑场)。

```json
{
  "type": "message",
  "subType": "reply",
  "msg_id": "...",
  "agentId": "agent-001",
  "sessionId": "main",
  "timestamp": "...",
  "priority": "high",
  "payload": {"content": "## 综述生成完毕\n\n..."}
}
```

订阅检查发现新论文(celery beat 触发)也走 `message/reply`(priority=high,离线触发 APNs)。

### 2.3 `tool/*` — 工具调用与进度

统一所有"Agent 正在做 X"的语义:子 Agent(ingest/citation_chase/...)、本地 CLI tool、iOS 端 tool。四种 subType 共享 `tool_call_id` 关联。

#### 2.3.1 `tool/start` — 启动

```json
{
  "type": "tool",
  "subType": "start",
  "msg_id": "...",
  "priority": "high",
  "payload": {
    "tool_call_id": "t1",
    "name": "ingest",
    "label": "论文搜索入库",
    "total_steps": 7,
    "can_cancel": true
  }
}
```

| 字段 | 说明 |
|------|------|
| `tool_call_id` | 本轮内唯一 ID,关联后续 progress/result |
| `name` | 子 Agent type 或 tool 名称 |
| `label` | 用户可见中文名 |
| `total_steps` | 可选,总步骤数(用于进度条) |
| `can_cancel` | 可选,默认 false。true 时 iOS 显示"取消"按钮(见 §4.2) |

#### 2.3.2 `tool/progress` — 进度

```json
{
  "type": "tool",
  "subType": "progress",
  "priority": "normal",
  "payload": {
    "tool_call_id": "t1",
    "step": 3,
    "total": 7,
    "stage": "download",
    "message": "下载 12/50"
  }
}
```

`step`/`total`/`stage`/`message` 均可选,按任务性质填。

#### 2.3.3 `tool/result` — 终态

```json
{
  "type": "tool",
  "subType": "result",
  "priority": "high",
  "payload": {
    "tool_call_id": "t1",
    "status": "done",
    "summary": "50 篇已入库,综述已生成",
    "data": {"totalPapers": 50, "downloaded": 50}
  }
}
```

`status` ∈ `done` | `failed`;`data` 可选,放结构化结果。

#### 2.3.4 `tool/call` — 请求 iOS 执行本地 tool

服务端发 `tool/call` 前**必须**检查目标 tool 是否在最近一条 inbound 消息的 `capabilities` 内。不在则跳过,改走 `message/reply` 提示用户"该操作需要在 App 内完成"。

```json
{
  "type": "tool",
  "subType": "call",
  "priority": "high",
  "payload": {
    "tool_call_id": "t2",
    "name": "ios_calendar_add",
    "input": {"title": "组会", "starts_at": "2026-06-24T14:00"}
  }
}
```

iOS 收到后执行本地 tool(可能弹系统授权弹窗,如 EventKit/CoreLocation 权限),完成后回 `tool_result`(见 §3.4)。当前 iOS 端 tool(9 个,注册于 [tool_registry.py](../../src/paper_search/agent/tool_registry.py)):`ios_file_read` / `ios_file_write` / `ios_file_list` / `ios_calendar_add` / `ios_calendar_read` / `ios_reminder_add` / `ios_notification_local` / `ios_device_info` / `ios_location_get`。

**v3.1 tool 命名规范**：

| 前缀 | 含义 | 示例 | WS 通知 |
|------|------|------|:---:|
| `agent_` | 子 Agent（独立 LangGraph 图，长任务） | `agent_search`, `agent_ingest`, `agent_survey` | `tool/start → progress → result` |
| `ios_` | iOS 端本地 tool | `ios_calendar_add` | `tool/call` → `tool_result` |
| 无前缀 | 主 Agent 本地 tool | `search_papers`, `ask_user` | 直接 `tool/result` |

从 LLM 视角，所有类型都是 tool_use，不需要区分。前缀仅用于服务端 dispatch 和 WS 通知策略。

### 2.4 `tool_execution` — 工具执行追踪 (v10.1 新增)

每次 tool 调用的独立消息，提供细粒度的执行追踪。与 `tool/start → tool/result` 互补：tool_execution 携带参数和结果摘要，适合执行历史视图。

```json
{
  "type": "tool_execution",
  "priority": "normal",
  "payload": {
    "tool_call_id": "tc-abc123",
    "todo_id": "todo-1",
    "name": "agent_search_papers",
    "status": "completed",
    "arguments": {"keywords": "transformer", "max_results": 20},
    "result_summary": "找到 50 篇论文，已入库 prj-001",
    "started_at": "2026-07-13T10:00:00Z",
    "completed_at": "2026-07-13T10:00:15Z"
  }
}
```

`status` ∈ `running` | `completed` | `failed`。`error` 字段仅在 `failed` 时有值。

### 2.5 `ask` — 用户操作入口(v10 核心)

**iOS 必须实现的唯一交互 tool**。所有需要用户授权或澄清的场景统一走 `ask`,通过 `kind` 字段切换五种渲染形态。带 `ask_id` 用于关联回答。

```json
{
  "type": "ask",
  "msg_id": "...",
  "priority": "high",
  "payload": {
    "ask_id": "ask-<uuid>",
    "kind": "confirm | choice | multi_choice | text | plan",
    "prompt": "<问题/方案标题>",
    "context": "<可选 ≤120 字>",
    "danger_level": "low | medium | high",

    "options": [{"value": "S1", "label": "文献调研", "hint": "搜索+筛选"}],

    "plan": {
      "scenario_id": "S2",
      "summary": "搜 50 篇 → 下载 → 综述",
      "permissions": ["search", "download"],
      "estimated_seconds": 1200,
      "steps": [{"label": "搜索", "detail": "arxiv+s2"}, {"label": "下载", "detail": "~15min"}]
    },

    "placeholder": "请输入关键词",
    "max_length": 200,

    "default": "<可选,choice/confirm 默认 value>",
    "timeout_seconds": 600
  }
}
```

**字段适用性**(按 kind):

| 字段 | confirm | choice | multi_choice | text | plan |
|------|:---:|:---:|:---:|:---:|:---:|
| `prompt` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `options[]` | — | ✓ | ✓ | — | — |
| `plan` | — | — | — | — | ✓ |
| `placeholder` / `max_length` | — | — | — | ✓ | — |
| `danger_level` / `timeout_seconds` | ✓ | ✓ | ✓ | ✓ | ✓ |

**`danger_level` 判定**(服务端按 scenario 权限硬映射,LLM 不参与,详见 §5.3):
- `low` = 纯查询(搜索 / 读摘要 / RAG 问答)→ **不弹 plan,直接执行**
- `medium` = 下载 PDF / 入库 / 订阅 → 弹 `ask(kind=plan)` 审批
- `high` = shell_exec / package_install / video_download / 批量删除 → 弹 `ask(kind=plan)`,iOS 按钮需二次确认

详见 §4(Ask Card 五形态)。

### 2.6 `error` — 错误

纯文本错误气泡,**无按钮**(用户重新打字描述需求即可)。

```json
{"type":"error","subType":"TASK_FAILED","priority":"urgent","payload":{"code":"TASK_FAILED","message":"搜索超时","correlation_id":"t1"}}
{"type":"error","subType":"INTERNAL_ERROR","priority":"urgent","payload":{"code":"INTERNAL_ERROR","message":"LLM 调用失败"}}
{"type":"error","subType":"ASK_TIMEOUT","priority":"urgent","payload":{"code":"ASK_TIMEOUT","message":"问题长时间未回答,已取消","correlation_id":"a1"}}
{"type":"error","subType":"AGENT_NOT_RUNNING","priority":"urgent","payload":{"code":"AGENT_NOT_RUNNING","message":"Agent 未运行，请先启动"}}
```

---

## 三、Inbound 消息详述

### 3.1 `ping`

```json
{"type":"ping","agentId":"agent-001","sessionId":"main","timestamp":"...","payload":{}}
```

### 3.2 `message` — 用户文本

```json
{"type":"message","agentId":"agent-001","sessionId":"main","timestamp":"...","capabilities":["calendar","location","file_read"],"payload":{"content":"搜索 transformer 论文"}}
```

> **注意**:每条 inbound 消息信封都带 `capabilities`(见 §3.6)。

### 3.3 `ask_reply` — 统一回答

**所有** Ask Card 的回执都用这一个 type,通过 `ask_id` 关联请求,`value` 按 kind 解码。

```json
{"type":"ask_reply","agentId":"agent-001","sessionId":"main","timestamp":"...","capabilities":[...],"payload":{"ask_id":"a1","value":true}}
```

`value` 解码规则(详见 §4.3):

| kind | value 类型 | 示例 |
|---|---|---|
| `confirm` / `plan` | `boolean` | `true` 批准 / `false` 拒绝(可带 `reason`) |
| `choice` | `string` | `"S1"` |
| `multi_choice` | `string[]` | `["S1","S12"]` |
| `text` | `string` | `"transformer attention"` |

拒绝时附原因:
```json
{"ask_id":"a1","value":false,"reason":"先不下载,只列表"}
```

### 3.4 `plan_approve` — 批准计划 (v10.1 新增)

```json
{"type":"plan_approve","agentId":"agent-001","sessionId":"main","payload":{"plan_id":"plan-a1b2c3d4e5f6"}}
```

### 3.5 `plan_revise` — 修改计划 (v10.1 新增)

```json
{"type":"plan_revise","agentId":"agent-001","sessionId":"main","payload":{"plan_id":"plan-a1b2c3d4e5f6","feedback":"只需要搜索，不用下载"}}
```

### 3.6 `tool_result` — 客户端 tool 执行结果

仅用于 `tool/call`(iOS 端 tool)的回执,通过 `tool_call_id` 关联。

```json
{"type":"tool_result","agentId":"agent-001","sessionId":"main","timestamp":"...","capabilities":[...],"payload":{"tool_call_id":"t2","status":"done","content":{"event_id":"..."}}}
```

`status` ∈ `done` | `failed`;`content` 是 tool 的任意返回值。

### 3.5 `capabilities` 信封字段

iOS 每条 outbound 消息(即 server 视角的 inbound)都带 `capabilities: string[]`,列出当前 App 可用的 iOS 端能力。服务端按 `agent_id+session_id` 缓存最新值。

| capability 值 | 对应 tool |
|---|---|
| `file_read` | `ios_file_read` / `ios_file_list` |
| `file_write` | `ios_file_write` |
| `calendar` | `ios_calendar_add` / `ios_calendar_read` |
| `reminder` | `ios_reminder_add` |
| `notification` | `ios_notification_local` |
| `device_info` | `ios_device_info` |
| `location` | `ios_location_get` |

服务端发 `tool/call` 前检查:目标 tool 对应的 capability 是否在列表内。不在则跳过该 tool,改走 `message/reply` 提示用户。

---

## 四、Ask Card 五形态

`ask` 是 iOS 必须实现的**唯一交互 tool**。五种 `kind` 共享同一卡片框架(标题 + 上下文 + 主体 + 操作区),主体按 kind 切换。

### 4.1 kind 矩阵

| kind | 用户看到 | `value` 类型 |
|---|---|---|
| `confirm` | 标题 + "批准 ✓ / 拒绝 ✗" 两个大按钮(底部可点"说明原因") | `boolean` |
| `choice` | 问题 + 单选列表(最多 6 项,超过滚动) | `string`(选项 value) |
| `multi_choice` | 问题 + 多选 chips + 底部"完成" | `string[]` |
| `text` | 问题 + 文本输入框 + "发送" | `string` |
| `plan` | 方案概要 + 权限标签 + 折叠步骤列表 + 预估时间 + "批准 / 拒绝" | `boolean` |

### 4.2 取消流程(可取消任务)

`tool/start` 带 `can_cancel=true` 时,Tool Card(running 态)显示"取消"按钮。点击后:

```
   ... 用户点 Tool Card 的"取消" ...
← ask (kind=confirm)   {"ask_id":"a4","kind":"confirm","danger_level":"low","prompt":"确认取消「论文搜索入库」任务?"}
→ ask_reply            {"ask_id":"a4","value":true}
← tool/result          {"tool_call_id":"t1","status":"failed","summary":"用户取消"}
← message/reply        {"content":"已取消。需要调整请告诉我。"}
```

### 4.3 `ask_reply.value` 解码

| kind | value 类型 | 备注 |
|---|---|---|
| `confirm` | `boolean` | `true`/`false`;`false` 可带 `reason` |
| `plan` | `boolean` | 同 confirm |
| `choice` | `string` | 选项的 `value` 字段(非 label) |
| `multi_choice` | `string[]` | 选中的 value 列表 |
| `text` | `string` | 用户输入文本 |

### 4.4 danger_level 硬映射

服务端按 scenario 的 `permissions_required` 查表判定,**LLM 不参与**:

| permissions | danger_level | 是否弹 plan |
|---|---|---|
| (空) / 仅查询类 | `low` | 否,直接执行 |
| `search` / `download` / `citation_chase` / `subscription` | `medium` | 是 |
| `shell_exec` / `package_install` / `video_download` / 含删除 | `high` | 是,iOS 按钮需二次确认 |

`danger_level` 同时影响 iOS 按钮配色:`high` 时"批准"为大红按钮 + 需长按 1s 确认。

---

## 五、Status 与调试通道

### 5.1 推荐 stage 值

`stage` 是**自由字符串**(非强制枚举),服务端按 handler 节点自由填。推荐值:

| stage | 含义 | 触发节点 |
|---|---|---|
| `received` | 收到用户消息 | MainAgent.run 入口(立即,<200ms) |
| `analyzing` | 意图识别中 | intent_classify |
| `searching` | 搜索中 | rag_handler / literature_search_handler |
| `executing` | 处理中 | ingest_handler / cleanup_handler |
| `responding` | 回复中 | inline_reply |
| `done` | 完成 | handler 完成 |
| `done` | 完成 | evaluate satisfied 后 |

子 Agent 内部更细的阶段(如 ingest 的 convert/index/rank)也可走 `status`,stage 自由命名(如 `"indexing"`/`"ranking"`)。

### 5.2 调试通道

`status.level` 区分可见性:

| level | 触发条件 | iOS 渲染 |
|---|---|---|
| `user` | 默认 | 正常显示(Status 气泡) |
| `debug` | `DEBUG_PROTOCOL=1` 环境变量 | **仅 iOS dev build 渲染**;生产 build 忽略 |

`level=debug` 的 status 用于排查内部编排问题(如"intent_classify LLM 返回了什么 JSON"),`message` 字段可放原始 LLM 输出摘要。生产环境服务端不发 debug status,生产 iOS 即便收到也不渲染——双保险。

#### 5.2.1 thinking_delta 思考过程可见 (v10.1 新增)

当 `DEBUG_PROTOCOL=1` 时，服务端将 LLM 的 **thinking 过程**（DeepSeek v4 Pro 等推理模型的 chain-of-thought）通过 `status{level:debug}` 推送给客户端：

```json
{
  "type": "status",
  "msg_id": "...",
  "agentId": "agent-001",
  "sessionId": "main",
  "timestamp": "...",
  "priority": "silent",
  "payload": {
    "stage": "llm:thinking_delta",
    "message": "{'thinking': 'We need to analyze the user query...'}",
    "level": "debug"
  }
}
```

| stage 值 | 含义 | 触发时机 |
|---|---|---|
| `llm:thinking` | LLM 返回完整 thinking block（非流式） | `_parse_response` 遇到 `type=thinking` 块 |
| `llm:thinking_delta` | LLM 流式返回思考 token | `chat_stream` 收到 `thinking_delta` SSE 事件 |
| `llm:tool_use` | LLM 调用 tool | `chat_stream` 收到 `tool_use_start` |

**行为**：生产环境（`DEBUG_PROTOCOL` 未设）不推任何 debug status；dev 环境推送但 iOS 生产 build 不渲染。debug 消息走 `priority=silent`，不持久化不 APNs。

#### 5.2.2 thinking 模式与 tool_choice 兼容性

DeepSeek v4 Pro 等推理模型的 thinking 模式 **不支持 `tool_choice`**。服务端自动处理：
- 普通对话（`chat`/`chat_stream`）：保持 thinking 模式，跳过 `thinking_delta`，等待 `text_delta` 返回最终文本
- 结构化输出（`chat_json` with `force_tool=True`）：自动添加 `thinking: {type: disabled}` 禁用思考模式

### 5.3 立即 ack 约束

MainAgent 收到用户 `message` 后,**必须**在 <200ms 内推 `status{stage:"received"}`,再开始 intent_classify。这保证用户看到 typing 指示器,不面对空白屏幕。

---

## 六、典型对话流程

### A. 综述生成(danger_level=medium → 弹 plan)

```
→ ping
← pong

→ message                     {"content":"做一个 self-supervised learning 综述,50 篇"}
                              capabilities:["calendar","location","file_read"]

← status                      {"stage":"received","message":"收到,正在分析...","level":"user"}
   ... intent_classify 内部跑(~5s,用户看不到 LLM 细节) ...
← status                      {"stage":"planning","message":"正在规划方案..."}
← ask (kind=plan)             {"ask_id":"a1","kind":"plan","danger_level":"medium",
                               "plan":{"scenario_id":"S2","summary":"搜 50 篇→下载→综述",
                                       "permissions":["search","download"],
                                       "estimated_seconds":1200,
                                       "steps":[{"label":"搜索","detail":"arxiv+s2"},
                                                {"label":"下载","detail":"~15min"},
                                                {"label":"综述","detail":"MD"}]}}

→ ask_reply                   {"ask_id":"a1","value":true}

← status                      {"stage":"searching","message":"开始搜索论文..."}
← tool/start                  {"tool_call_id":"t1","name":"ingest","label":"论文搜索入库","total_steps":7,"can_cancel":true}
← tool/progress               {"tool_call_id":"t1","step":1,"total":7,"stage":"search","message":"找到 64 篇"}
← tool/progress               {"tool_call_id":"t1","step":3,"total":7,"stage":"download","message":"下载 12/50"}
← tool/result                 {"tool_call_id":"t1","status":"done","summary":"50 篇已入库,综述已生成"}
← status                      {"stage":"done","message":"完成"}
← message/reply               {"content":"## 综述生成完毕\n\n..."}
```

### B. 灰区澄清(C3,danger_level=low → 不弹 plan)

```
→ message                     {"content":"找点论文"}
← status                      {"stage":"received","message":"收到,正在分析..."}
← status                      {"stage":"analyzing","message":"意图有点模糊..."}
← ask (kind=multi_choice)     {"ask_id":"a2","kind":"multi_choice","danger_level":"low",
                               "prompt":"想要哪种?可多选",
                               "options":[{"value":"S1","label":"文献调研","hint":"快速找几篇"},
                                          {"value":"S2","label":"综述生成","hint":"批量+写综述"},
                                          {"value":"__none__","label":"都不是 / 重新描述"}]}
→ ask_reply                   {"ask_id":"a2","value":["S1"]}
   ... danger_level=low 不弹 plan,直接执行 ...
← tool/start → tool/progress → tool/result
← message/reply               {"content":"找到 12 篇相关论文..."}
```

### C. iOS-side tool(加日历)

```
→ message                     {"content":"把明天的组会加到日历"}
                              capabilities:["calendar","location","file_read"]

← status                      {"stage":"received","message":"收到..."}
← ask (kind=confirm)          {"ask_id":"a3","kind":"confirm","danger_level":"medium",
                               "prompt":"将添加日历事件:组会 / 明天 14:00-15:00"}
→ ask_reply                   {"ask_id":"a3","value":true}

← tool/start                  {"tool_call_id":"t2","name":"ios_calendar_add","label":"加入日历"}
← tool/call                   {"tool_call_id":"t2","name":"ios_calendar_add",
                               "input":{"title":"组会","starts_at":"2026-06-24T14:00"}}
   ... iOS 弹系统授权 → EventKit 写入 ...
→ tool_result                 {"tool_call_id":"t2","status":"done","content":{"event_id":"..."}}
                              capabilities:["calendar","location","file_read"]
← tool/result                 {"tool_call_id":"t2","status":"done","summary":"已加入日历"}
← message/reply               {"content":"已添加「组会」到明天 14:00。"}
```

### D. 取消运行中任务

```
   ... t1 正在 download 阶段,用户点 Tool Card 的"取消" ...
← ask (kind=confirm)          {"ask_id":"a4","kind":"confirm","danger_level":"low",
                               "prompt":"确认取消「论文搜索入库」任务?"}
→ ask_reply                   {"ask_id":"a4","value":true}
← tool/result                 {"tool_call_id":"t1","status":"failed","summary":"用户取消"}
← message/reply               {"content":"已取消。需要调整请告诉我。"}
```

### E. 订阅推送(celery beat 触发)

```
← message/reply               {"content":"📡 订阅《self-supervised》今日新增 3 篇:\n\n1. ..."}
   // priority=high → 用户离线时触发 APNs
```

### F. 并行子 Agent(复合意图 S1+S12)

```
→ message                     {"content":"找几篇 transformer 论文,顺便翻译标题"}
← status                      {"stage":"received","message":"收到,正在分析..."}
← status                      {"stage":"planning","message":"规划中..."}
   ... danger_level=low,不弹 plan,直接执行两个并行子 Agent ...
← tool/start                  {"tool_call_id":"t1","name":"ingest","label":"论文搜索"}
← tool/start                  {"tool_call_id":"t2","name":"translation","label":"标题翻译"}
← tool/progress               {"tool_call_id":"t1","stage":"search","message":"找到 8 篇"}
← tool/progress               {"tool_call_id":"t2","stage":"translate","message":"翻译 3/8"}
← tool/result                 {"tool_call_id":"t1","status":"done","summary":"8 篇已入库"}
← tool/result                 {"tool_call_id":"t2","status":"done","summary":"8 个标题已翻译"}
← message/reply               {"content":"找到 8 篇 transformer 论文,标题已翻译:\n\n..."}
```

iOS 渲染:两张 Tool Card 纵向堆叠,各自独立进度,可分别展开/折叠。

---

## 七、APNs 离线推送规则

离线时(无活跃 WS 连接),服务端按 `priority` 决定是否触发 APNs:

| priority | 在线 | 离线 |
|---|---|---|
| `silent` | WS 推 | **丢弃** |
| `normal` | WS 推 + 持久化 | 持久化(不 APNs) |
| `high` | WS 推 + 持久化 | 持久化 + APNs(带预览) |
| `urgent` | WS 推 + 持久化 | 持久化 + APNs(带响铃) |

### 7.1 Ask 超时 APNs

`ask` 卡片发出后 `timeout_seconds`(默认 600s)用户未回:
1. 推一条 APNs"有个问题等你回答"(带 ask 摘要预览)。
2. 再等一个 `timeout_seconds` 周期。
3. 仍未回 → 发 `error/ASK_TIMEOUT`,Agent 按"用户放弃"处理(取消该 plan 或降级为 chat)。iOS 卡片变灰不可再点。

### 7.2 APNs 注册

iOS 启动时通过 `POST /api/devices/register` 注册 APNs device token:

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

## 八、v9.1 → v10 迁移表

| v9.1 | v10 | 备注 |
|------|-----|------|
| `message/chat` (in) | `message` (in) | 去 subType |
| `message/text` (out) | `message/reply` | 重命名 |
| `message/thinking` (out) | **删除** | 内部 CoT 不再暴露;阶段反馈走 `status` |
| `tool/ios_request` | `tool/call` | 统一 tool 系列 |
| `tool/ios_result` (in) | `tool_result` (in) | 升为顶级 inbound |
| `tool/ask_user_question` | `ask`(kind=choice\|multi_choice\|text) | **三合一** |
| `tool/propose_plan` | `ask`(kind=plan) | **合并** |
| `tool/sub_request` | `tool/start` | 统一"启动"语义 |
| `tool/sub_progress` | `tool/progress` | 统一"进度"语义 |
| `tool/sub_result` | `tool/result` | 统一"结果"语义 |
| (无) | **`status`(新增)** | 人类可读阶段更新;替代 thinking 的"反馈空档"职责 |
| (无) | **`capabilities` 信封字段(新增)** | iOS 每条带能力上报 |
| `sync_request` | **已移除** (v10.2) | 改为 REST API |
| `sync_complete` | **已移除** (v10.2) | 改为 REST API |
| `error/*` | `error`(subType 保留 code)+ 新增 `ASK_TIMEOUT` | 保留 |
| `priorityKind` | `priority` | 缩短 |
| `role` | **删除** | v10 标记, v11.2 正式移除 |

### v10 → v11.2 (v5 架构) 变更

| v10 | v11.2 | 备注 |
|-----|-------|------|
| `plan_review` (out) | **删除** | v5 无规划节点, 审批走 `ask(kind=plan)` |
| `plan_todo_update` (out) | **删除** | v5 无 todo 列表 |
| `gate` (out) | **删除** | v5 无 gate 节点 |
| `plan_approve` (in) | **删除** | 无审批流程 |
| `plan_revise` (in) | **删除** | 无审批流程 |
| `role` (envelope) | **正式删除** | 代码已移除 |

**消息总数**: v10 的 12 种 → outbound 8 + inbound 4 = 12 种。

### 向后兼容

过渡期服务端可同时发 v9.1 + v10 字段(如同时带 `priorityKind` 和 `priority`),iOS 优先识别 v10。旧 iOS 客户端忽略 v10 新字段(`status` / `capabilities`)仍可工作,但失去阶段反馈与能力上报能力。建议一个版本后切纯 v10。

---

## 九、附录:服务端代码改造 checklist

> 本文档只定义协议;以下代码改造在另立 PR 执行。

| 文件 | 改动 |
|------|------|
| [src/paper_search/agent/outbox.py](../../src/paper_search/agent/outbox.py) | `PRIORITY_DEFAULTS` 换 v10 的 `(type,subType)`;`priorityKind` → `priority` |
| [src/paper_search/agent/main_agent.py](../../src/paper_search/agent/main_agent.py) | ① 收到 user message 后立即 `_push status{stage:"received"}`(<200ms);② intent/plan/eval 各节点入口/出口推 `status{stage:"analyzing"/"planning"/...}`(level=user);③ 内部 LLM raw 输出在 `DEBUG_PROTOCOL=1` 时推 `status{level:debug}`;④ 所有 `tool/ask_user_question`/`tool/propose_plan`/`tool/ios_request`/`tool/sub_*` 改成 `ask` / `tool/start\|progress\|result\|call`;⑤ `propose_plan` 按 danger_level 硬映射决定是否发(低危跳过审批直接 execute);⑥ Ask 超时 APNs 提醒 + 二次超时发 `error/ASK_TIMEOUT` |
| [main_agent.py](../../src/paper_search/agent/main_agent.py) `_wait_ws_reply` | 等 `ask_reply`(by `ask_id`) 或 `tool_result`(by `tool_call_id`),不再按 (type,subType) 匹配 |
| [src/paper_search/api/app.py](../../src/paper_search/api/app.py) 主消息循环 | 加 `ask_reply`/`tool_result` 入站 handler;从信封读 `capabilities` 缓存;旧类型做 alias 一个版本过渡;sync 改为 REST API |
| [src/paper_search/api/ws.py](../../src/paper_search/api/ws.py):222 (subscription) | `priority:2` → `priority:"high"`,加 `msg_id`;类型从 `subscription/new_papers` 改为 `message/reply` |
| [src/paper_search/api/outbox_poller.py](../../src/paper_search/api/outbox_poller.py):124 | 兼容 `priority` 新字段;APNs 预览按新 type/subType 分支;Ask 超时 APNs 逻辑 |
| [src/paper_search/agent/main_agent_prompts.py](../../src/paper_search/agent/main_agent_prompts.py) | `ClarificationQuestion` 加 `values`+`hint`;`ScenarioPlanResult` 加 `danger_level` 字段(供硬映射校验);`ToolCallSpec` 的 `kind=ios_tool` 保留 |
| [src/paper_search/agent/tool_registry.py](../../src/paper_search/agent/tool_registry.py) | 9 个 `ios_*` tool 保留;新增 scenario→danger_level 映射表(按权限硬映射,见 §4.4) |

### 新增环境变量

| Variable | Purpose |
|---|---|
| `DEBUG_PROTOCOL` | `=1` 时服务端推 `status{level:debug}` 消息(iOS dev build 渲染);默认不推 |
| `ASK_TIMEOUT_SECONDS` | Ask 卡片首次超时阈值(默认 600);超时后推 APNs 提醒一次,再等一个周期发 `error/ASK_TIMEOUT` |
