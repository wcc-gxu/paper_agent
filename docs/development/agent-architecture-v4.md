# Paper Agent v4.2 — Agent 架构设计文档

> 最后更新: 2026-07-19
> 状态: v4.1 已进入开发；v4.2 统一状态管理 + 双向 Pub/Sub 控制已上线
> 来源: Claude Code 架构对话 + OpenClaw 竞品分析 + 多轮需求确认

---

## 1. 概述

### 1.1 进程架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Supervisor 容器 (daemon)                       │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Agent Supervisor                                           ││
│  │  - AgentStateManager: Redis agent:state:{agent_id} Hash      ││
│  │  - LifecycleLogger: ~/.paper_search/logs/agent_lifecycle.jsonl││
│  │  - BRPOP agent:ws:{uid} → stdin → Agent 子进程               ││
│  │  - Agent 子进程 stdout → LPUSH agent:outbox:{uid}            ││
│  │  - 3 层健康检测 (poll/积压/stdout)                            ││
│  │  - 双向 Pub/Sub: agent:control + agent:control:resp:{corr_id} ││
│  └─────────────────────────────────────────────────────────────┘│
│         │ stdin/stdout pipe          │ stdin/stdout pipe         │
│  ┌──────▼──────────┐         ┌──────▼──────────┐                │
│  │ Agent 子进程     │         │ Agent 子进程     │   ...          │
│  │ user-abc        │         │ user-xyz        │                │
│  │ PID=1001        │         │ PID=1002        │                │
│  │                 │         │                 │                │
│  │ 不直接连 Redis   │         │ 不直接连 Redis   │                │
│  │ 仅 stdin/stdout │         │ 仅 stdin/stdout │                │
│  │                 │         │                 │                │
│  │ intent_classify │         │ intent_classify │                │
│  │   → plan ⇄ clarify → gate → Celery execute  │                │
│  │                                                 │             │
│  └─────────────────────────────────────────────────┘             │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │  Celery Worker 容器                                          ││
│  │  重型任务: 论文搜索/下载/转换/向量化/综述/视频                   ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Supervisor → Agent 通信模型

Agent 子进程**不直接连接 Redis**。所有进出消息通过 stdin/stdout pipe 经由 Supervisor 中转：

```
入站: API → LPUSH agent:ws:{uid} → Supervisor BRPOP → Agent stdin
出站: Agent stdout → Supervisor → LPUSH agent:outbox:{uid} → API BRPOP → WS
状态: Agent stdout {"type":"state","state":"busy"} → Supervisor → HSET agent:state:{agent_id}
控制 (v4.2 REST): API SUBSCRIBE agent:control:resp → PUBLISH agent:control → Supervisor → PUBLISH resp
控制 (v4.2 SSE):  API SUBSCRIBE agent:sse:{corr_id} → PUBLISH agent:control → Supervisor → PUBLISH event* → done
```

### 1.3 关键设计变更

| 概念 | v4.0 草案 | v4.1 最终 | v4.2 | 原因 |
|------|--------|--------|------|------|
| Agent 进程模型 | daemon 内 asyncio.Task | **每用户独立子进程** | 同 v4.1 | OS 级隔离，崩溃互不影响 |
| Agent 连 Redis | Agent 直连 | **Agent 不连 Redis** | 同 v4.1 | 简化 Agent、Gateway 可拦截 |
| 心跳机制 | Agent → Redis heartbeat key | **Supervisor 3 层检测** | 同 v4.1 | Agent 无需主动上报 |
| API 查状态 | GET Redis heartbeat key | **HGET agent:status Hash** | **agent:state:{agent_id} Hash** | 每条记录包含完整 agent 配置 |
| Pub/Sub 控制 | 单向上报 + 控制 | **仅控制指令 (单向)** | **双向请求-响应** | API 等待启动确认 (180s 超时) |
| 状态管理 | DB + Redis 双写 | Supervisor 写 Redis | **AgentStateManager 统一管理** | Redis + DB + lifecycle 日志三方一致 |
| 创建 Agent | 注册时创建 | 注册时创建 | **Supervisor 自动创建** | API 发 start 时 Redis/DB 无记录自动建 |
| 生命周期日志 | 无 | 无 | **JSONL 结构化日志** | agent.create/launch/crash/stop 全事件可追踪 |
| 客户端连接 | 轮询 status | 轮询 status | **SSE 事件流** | start/stop/restart 实时推送进度，无超时 |

### 1.4 SSE 控制流 (v4.2 新增)

替代原有的同步 HTTP 请求-响应模式。API 通过 SSE 长连接将 Supervisor 的生命周期事件实时推送给客户端。

```
┌─ SSE 启动流程 ──────────────────────────────────────────────────────────┐
│                                                                         │
│  Client                     API                        Supervisor       │
│   │                          │                            │             │
│   │ GET /.../start/stream    │                            │             │
│   │  Authorization: Bearer   │                            │             │
│   │  Accept: text/event-     │                            │             │
│   │          stream          │                            │             │
│   │ ─────────────────────────→                            │             │
│   │                          │ SUBSCRIBE agent:sse:{corr} │             │
│   │                          │ PUBLISH agent:control      │             │
│   │                          │ ───────────────────────────→             │
│   │                          │                            │             │
│   │                          │◄─── agent:sse:{corr} ──────│             │
│   │ event: state             │   {"event":"state",        │             │
│   │ data: {"to":"starting"}  │    "to":"starting"}        │             │
│   │ ◄────────────────────────┤                            │             │
│   │                          │◄─── agent:sse:{corr} ──────│             │
│   │ event: progress          │   {"event":"progress",     │             │
│   │ data: {"stage":"launch"} │    "stage":"launch"}       │             │
│   │ ◄────────────────────────┤                            │             │
│   │                          │◄─── agent:sse:{corr} ──────│             │
│   │ event: state             │   {"event":"state",        │             │
│   │ data: {"to":"idle"}      │    "to":"idle","pid":123}  │             │
│   │ ◄────────────────────────┤                            │             │
│   │                          │◄─── agent:sse:{corr} ──────│             │
│   │ event: done              │   {"event":"done",         │             │
│   │ data: {"status":"started"}│  "status":"started"}      │             │
│   │ ◄────────────────────────┤                            │             │
│   │ (连接关闭)                │                            │             │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**SSE 事件类型**：

| event | data | 触发时机 | 连接关闭 |
|-------|------|---------|:---:|
| `state` | `{from, to, agent_id, pid?}` | 状态机任意转移 | 否 |
| `progress` | `{stage, message, elapsed_ms?}` | 操作中的阶段性进度 | 否 |
| `done` | `{status, agent_state?, ...}` | 操作成功完成 | **是** |
| `error` | `{error, agent_state?}` | 操作失败 | **是** |

**端点**：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/agents/me/start/stream` | GET | 启动 Agent → SSE 流 |
| `/api/agents/me/stop/stream` | GET | 停止 Agent → SSE 流 |
| `/api/agents/me/restart/stream` | GET | 重启 Agent → SSE 流 |
| `/api/agents/me/start` | POST | 同步启动（兼容保留） |
| `/api/agents/me/stop` | POST | 同步停止（兼容保留） |

**协议特点**：
- **不设超时** — SSE 连接持续打开直到 done/error 或客户端断开
- **透明透传** — API 不作处理，直接转发 Supervisor 事件
- **幂等安全** — 重复 start/stop 返回 `already_running` / `already_stopped`

### 1.5 7 种子意图（不变）

| 类型 | 子意图 | 工具范围 | 说明 |
|------|--------|---------|------|
| non-research | `chat` | None | 闲聊/知识问答 → flash reply |
| non-research | `ops` | docker_*, system_* | 运维操作（admin only）→ ops_plan |
| research | `survey` | search, ingest | 文献调研 |
| research | `kb_retrieval` | kb_search, kb_ask | 知识库检索/问答 |
| research | `paper_analysis` | paper_read, kb_extract | 单篇论文精读 |
| research | `writing` | doc_* | AI 辅助写作 |
| research | `knowledge_mgmt` | ingest_url, subscription | 知识管理 |

---

## 2. 代理节点设计（不变）

| 节点 | LLM | 所在进程 | 输出 | 说明 |
|------|-----|---------|------|------|
| `intent_classify` | Flash (≈2s) | Agent 子进程 | `intents[]`, `planning_prompt`, `should_plan` | 7 意图独立打分 |
| `chat_reply` | Flash/Pro | Agent 子进程 | `message/reply` | 纯对话直接回答 |
| `ops_plan` | Flash (≈2s) | Agent 子进程 | plan JSON → Celery | 运维规划，无 review |
| `plan_node` | Pro | Agent 子进程 | `{ needs_clarification, plan? }` | 研究规划 |
| `clarify_node` | Pro | Agent 子进程 | `collected_info` | ReAct ≤5 轮 |
| `react_execute` | Pro | **Celery Worker** | `message/reply` | 执行已批准计划 |

---

## 3. Agent 状态机

### 3.1 8 种状态

```
                    ┌──────────┐
      首次创建/发现  │ PENDING  │
          ─────────→│(未启动)   │
                    └────┬─────┘
                         │ API: start
                    ┌────▼─────┐
         API: start │ STARTING │ crash/error
         ──────────→│          │──────────────┐
                    └────┬─────┘              │
                         │ worker idle        ▼
                    ┌────▼─────┐        ┌──────────┐
                    │   IDLE   │◄───────│ CRASHED  │
                    │(无活跃turn)│ 自动重启 │          │
                    └────┬─────┘        └──────────┘
         WS消息到达      │  turn完成
                    ┌────▼─────┐
                    │   BUSY   │
                    │active>0  │
                    └────┬─────┘
                         │
         API: stop  ┌────▼─────┐         ┌──────────┐
         ──────────→│ STOPPING │         │ STALLED  │
                    └────┬─────┘         │(idle但队列│
                         │               │积压>0.5d) │
                    ┌────▼─────┐         └──────────┘
                    │ STOPPED  │
                    └──────────┘
```

| 状态 | 含义 | 触发 | 前端 |
|------|------|------|------|
| `pending` | Agent 已注册，尚未首次启动 | Supervisor 自动创建 / API 注册 | 灰色 ● 未启动 |
| `starting` | 子进程已 launch，初始化中 | API 调 start | 加载中 |
| `idle` | 正常运行，无活跃 turn | turn 完成 | 绿色 ● 在线 |
| `busy` | 正在处理消息 | WS 消息到达 | 黄色 ● 处理中 |
| `stopping` | 收到停止信号，清理中 | API 调 stop | 停止中 |
| `stopped` | 正常退出 | Agent 进程 exit 0 | 灰色 ● 已停止 |
| `crashed` | 异常退出 | Agent 进程 exit ≠ 0 | 红色 ● 异常 |
| `stalled` | idle 但队列积压 > 0.5d | Supervisor 队列检测 | 橙色 ● 卡死 |

### 3.2 状态上报路径

```
路径 1: Agent → stdout → Supervisor → AgentStateManager.transition()
         → Redis agent:state:{agent_id} + lifecycle.jsonl + DB(异步)

  Agent 内部在 LangGraph 节点转换时:
    print({"type":"state","state":"busy","node":"plan","startup_ms":0})

路径 2: Supervisor OS 检测 → AgentStateManager.transition()

  proc.returncode is not None → state=crashed/stopped

路径 3: Supervisor 队列检测 → AgentStateManager.update()

  state=idle + 队列最老消息 > 43200s → state=stalled
  
路径 4 (v4.2): API 控制 → AgentStateManager.transition()

  API start → pending→starting / API stop → stopping→stopped
```

### 3.3 Busy 状态超时检测（按节点）

| Agent 报告的 node | 合理超时 | 超时后动作 |
|-------------------|:---:|------|
| `intent_classify` | 2min | SIGTERM → 重启 |
| `plan` | 2min | SIGTERM → 重启 |
| `execute` | 5min | SIGTERM → 重启 |
| `clarify` | 5min | SIGTERM → 重启 |
| `gate` / `ask_user` | 10min | SIGALRM → Agent 内部处理超时 |
| `evaluate` | 2min | SIGTERM → 重启 |
| `inline_reply` | 1min | SIGTERM → 重启 |

**用户 review 超时：** Agent 内部处理 `ASK_TIMEOUT`（已有机制），Supervisor 不发 SIGTERM，避免误杀正在等待用户输入的进程。

---

## 4. 3 层健康检测（Supervisor 外部监控）

Agent 不主动发心跳。Supervisor 从外部检测：

| 层 | 方式 | 延迟 | 检测什么 |
|:---:|------|:---:|------|
| 1 | `proc.poll()` / `returncode` | 即时（进程退出时 event loop 通知） | 进程死了吗 |
| 2 | 队列积压 + state 对比 | ≤30s 周期 | idle 但不消费消息 |
| 3 | stdout 最后更新时间 | ≤15s | 是否还有输出 |

```python
# Supervisor 检测逻辑
async def _monitor_loop(self):
    while True:
        for uid, proc in self.agents.items():
            # Layer 1: OS process
            if proc.returncode is not None:
                state = "crashed" if proc.returncode != 0 else "stopped"
                await self._hsync_status(uid, state=state)
                if self._should_restart(uid, proc.returncode):
                    asyncio.create_task(self._relaunch(uid))
                continue

            info = self.status_cache.get(uid, {})
            state = info.get("state")
            node = info.get("node", "")

            # Layer 2: busy timeout
            if state == "busy":
                timeout = NODE_TIMEOUTS.get(node, 300)
                if now - info["updated"] > timeout:
                    proc.send_signal(signal.SIGTERM)

            # Layer 2: idle queue staleness
            if state == "idle":
                oldest = await redis.xrange(f"agent:ws:{uid}", count=1)
                if oldest and now - oldest.timestamp > 43200:  # 0.5d
                    await self._hsync_status(uid, state="stalled")

            # Layer 3: stdout silent
            if now - info["updated"] > 15:
                logger.warning(f"Agent {uid} stdout silent for {now - info['updated']}s")

        await asyncio.sleep(10)
```

---

## 5. 工具分类：in-process vs Celery

### 5.1 Agent 进程内（asyncio / sync）

执行时间 <10s，内存占用小，失败不影响进程稳定性：

| 类别 | 工具 | 执行方式 |
|------|------|:---:|
| 文件读写 | read_file, write_file, edit_file, glob_files | sync / to_thread |
| 内容搜索 | grep_content, web_search, web_fetch | async |
| 文档 (v4.0) | doc_read, doc_write_section, doc_append, doc_diff_apply, doc_generate_review, doc_search_rag | sync |
| 记忆 & 偏好 | search/summarize/delete_memory, get_user_preference | sync |
| DB 轻量读 | paper_status, list_sources, get_paper_abstract | sync |
| 订阅管理 | create/list/delete subscription | sync |
| Zotero | zotero_export, zotero_import | sync |
| iOS 工具 | ios_* (9 个) | sync (等客户端回复) |

### 5.2 Celery Worker 容器

执行时间 >10s，CPU/内存密集，或需要资源隔离：

| 类别 | 工具 | 原因 |
|------|------|------|
| 论文搜索/下载 | agent_search_papers, agent_download_paper | 网络 IO + 可能超时 |
| PDF 转换 | agent_convert_paper | CPU 密集，可能 OOM |
| 向量化入库 | agent_index_paper | 批量 embedding API |
| 综述生成 | agent_generate_survey | 长 LLM 调用 |
| 知识库 | agent_knowledge_ingest/ask/extract | 批量 chunk + LLM |
| 引用分析 | agent_citation_chase, agent_clustering | 爬虫 + 计算 |
| 翻译/术语 | agent_translate, agent_build_glossary | 批量 LLM |
| 视频 | agent_capture_video | 下载 + ASR |
| Docker/系统 | docker_compose_*, apt_install, pip_install | 系统级操作 |

### 5.3 子 Agent 进度上报

Celery 任务**不直接写 outbox**。结果通过 Agent 内部 asyncio.Queue 中继，Agent 统一 stdout 上报：

```
Celery 报告进度 → Agent 内部 Queue → Agent stdout → Supervisor → LPUSH outbox:{uid}
```

---

## 6. 进程模型与隔离

### 6.1 进程管理（Supervisor → Agent）

```python
# 创建
proc = await asyncio.create_subprocess_exec(
    sys.executable, "-m", "paper_search.agent.agent_worker",
    "--user-id", uid,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)

# 停止（优雅）
proc.send_signal(signal.SIGTERM)
await asyncio.sleep(5)
if proc.returncode is None:
    proc.kill()  # 强制

# 检测
proc.returncode  # None = 运行中, int = 退出码
```

### 6.2 隔离保证

| 层级 | 机制 | 失败影响 |
|------|------|:---:|
| 用户间 | OS 进程隔离（独立 PID + 独立内存空间） | ✅ 零影响 |
| 子 Agent（Celery）| 独立容器 + 独立 Python 进程 | ✅ 零影响（Agent 收到异常） |
| 工具间（同一 Agent 内）| asyncio.Task + `asyncio.wait_for(task, timeout)` | ⚠️ 超时兜底 |

### 6.3 Agent 内部并发

```python
# Agent 内部并发工具调用
results = await asyncio.gather(
    asyncio.wait_for(tool_a.execute(), timeout=300),
    asyncio.wait_for(tool_b.execute(), timeout=300),
    return_exceptions=True,
)
# 每个 Task 独立监控: task.done(), task.exception()
```

---

## 7. 幂等与重试

| 层级 | 幂等键 | 实现 |
|------|--------|------|
| 用户消息 | `msg_id` | API 缓存 "已接收" → 不重复入队 |
| Agent turn | `correlation_id` | Agent 执行前查缓存，有 → 直接返回已有结果 |
| Celery task | `task_id` + `acks_late=True` | 完成后 ack，崩溃不重复执行 |
| 工具调用 | `tool_call_id` | outbox 已有去重索引 |

---

## 8. Redis Key 设计

| Key | 类型 | 写 | 读 | 用途 |
|-----|------|:---:|:---:|------|
| `agent:state:{agent_id}` | **Hash** | Supervisor | API / WS | 每个 agent 完整状态（配置 + 运行时） |
| `agent:active` | **SET** | Supervisor | Supervisor | 所有非 stopped 的 agent_id |
| `agent:ws:{uid}` | **List** | API | Supervisor | 入站消息队列 |
| `agent:outbox:{uid}` | **List** | Supervisor | API outbox_poller | 出站消息队列 |
| `agent:control` | **Pub/Sub** | API | Supervisor | 控制指令 (cmd, agent_id, user_id, correlation_id) |
| `agent:control:resp:{corr_id}` | **Pub/Sub** | Supervisor | API | 控制响应 (REST 同步模式) |
| `agent:sse:{corr_id}` | **Pub/Sub** | Supervisor | API | SSE 事件流 (新) — 操作进度实时推送 |
| `agent:ws:{uid}:parked` | **List** | Supervisor | Supervisor | 未匹配消息暂存 |

### agent:state:{agent_id} Hash 结构 (v4.2)

每个 agent 一个独立 Hash，包含完整配置和运行时状态：

```json
// HSET agent:state:agent-user-abc
{
  // ── 身份 ──
  "agent_id": "agent-user-abc",
  "user_id": "user-abc",
  "agent_type": "main",
  "display_name": "我的科研助理",

  // ── LLM 配置 ──
  "llm_provider": "deepseek",
  "llm_model": "deepseek-v4-pro",
  "system_prompt": "",

  // ── 运行时配置 ──
  "checkpoint_backend": "",
  "session_default": "main",
  "iteration_limit": "8",
  "user_timeout_seconds": "1800",
  "message_window_trim_max_tokens": "8000",
  "data_dir": "~/.paper_search",

  // ── 用户偏好 ──
  "user_preferences": "{\"research_domain\":\"AI\",\"writing_style\":\"APA\",\"language_pref\":\"zh\"}",

  // ── 运行时状态 ──
  "state": "idle",
  "pid": "12345",
  "current_node": "",
  "active_turns": "0",
  "current_session_id": "main",
  "started_at": "2026-07-19T10:00:00Z",
  "last_active_at": "2026-07-19T10:05:23Z",
  "last_error": "",
  "exit_code": "0",
  "restart_count": "0",
  "created_at": "2026-07-19T09:00:00Z",
  "updated_at": "2026-07-19T10:05:23Z"
}
```

**API 查询**：`HGETALL agent:state:agent-user-abc` — 返回完整配置 + 状态，1 次 Redis 往返。

**API 查状态**：`HGET agent:state:agent-user-abc state` — 仅查状态字段。

### agent:active SET

```bash
SMEMBERS agent:active
# → ["agent-user-abc", "agent-user-xyz"]
```

Supervisor 自动维护：`transition()` 到 terminal 时 `SREM`，离开 terminal 时 `SADD`。

---

## 9. 消息全链路 (v4.2)

```
┌─ 入站 ─────────────────────────────────────────────────────────────────┐
│  1. Client → WS → FastAPI                                              │
│  2. API: HGET agent:state:{agent_id} state → running? idle?           │
│     NO → error/AGENT_NOT_RUNNING                                       │
│  3. API: LPUSH agent:ws:{uid}                                          │
│  4. Supervisor: BRPOP agent:ws:{uid}                                  │
│  5. Supervisor: proc.stdin.write(msg)                                 │
│  6. Agent: stdin.readline → process → print(result)                   │
├────────────────────────────────────────────────────────────────────────┤
│  7. Supervisor: stdout.readline → parse JSON                          │
│  8. Supervisor: LPUSH agent:outbox:{uid} (内容不变)                    │
│  9. API outbox_poller: BRPOP agent:outbox:{uid}                       │
│ 10. API: ws_manager.broadcast → Client                                │
├─ 状态同步 ────────────────────────────────────────────────────────────┤
│  Agent: print({"type":"state","state":"busy"})                        │
│  Supervisor → AgentStateManager.transition("busy") →                  │
│  HSET agent:state:{agent_id} + lifecycle.jsonl + DB(异步)             │
├─ 控制 (v4.2 REST 同步) ────────────────────────────────────────────────────┤
│  API: SUBSCRIBE agent:control:resp:{corr_id}                              │
│  API: PUBLISH agent:control {cmd, agent_id, user_id, correlation_id}      │
│  Supervisor: 处理 → AgentStateManager.transition() →                      │
│  Supervisor: PUBLISH agent:control:resp:{corr_id} {status, agent_state}   │
│  API: 收到响应 → UNSUBSCRIBE → return to client                           │
├─ 控制 (v4.2 SSE 实时) ─────────────────────────────────────────────────────┤
│  API: SUBSCRIBE agent:sse:{corr_id}                                       │
│  API: PUBLISH agent:control {cmd, agent_id, user_id, correlation_id}      │
│  Supervisor: 处理 → 每步 PUBLISH agent:sse:{corr_id} {event, data}        │
│  API: stream SSE event → Client (不设超时，连接保持直到 done/error)       │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 10. 反幻觉体系（不变）

三层防线：人格设定 → 上下文质量（主战场）→ 规则验证（兜底）。详见 [anti-hallucination.md](anti-hallucination.md)。

---

## 11. 决策记录

| # | 决策 | 日期 | 版本 |
|---|------|------|------|
| 1 | 每用户 1 个 Agent 子进程（OS 级隔离） | 2026-07-18 | v4.1 |
| 2 | Agent 子进程不连 Redis，仅 stdin/stdout pipe 通信 | 2026-07-18 | v4.1 |
| 3 | Supervisor 3 层健康检测替代心跳（poll/积压/stdout） | 2026-07-18 | v4.1 |
| 4 | API 通过 Redis Hash `agent:state:{agent_id}` 查询完整状态 | 2026-07-19 | v4.2 |
| 5 | 双向 Pub/Sub 控制协议 (agent:control + agent:control:resp) | 2026-07-19 | v4.2 |
| 6 | 工具二分：轻工具 Agent 内执行，重工具 Celery Worker | 2026-07-18 | v4.1 |
| 7 | AgentStateManager 统一状态管理 (Redis + DB + LifecycleLogger) | 2026-07-19 | v4.2 |
| 8 | Supervisor 自动创建 Agent（Redis/DB 无记录时） | 2026-07-19 | v4.2 |
| 9 | 结构化生命周期日志 (agent_lifecycle.jsonl) | 2026-07-19 | v4.2 |
| 10 | API 启停同步等待确认 (start 180s / stop 30s 超时) | 2026-07-19 | v4.2 |
| 11 | SSE 事件流替代轮询 — start/stop/restart 实时推送进度，无超时 | 2026-07-19 | v4.2 |
| 7 | Celery 进度上报经 Agent stdout 中继，不直接写 outbox | 2026-07-18 |
| 8 | Intent classify 7 意图独立打分，Flash 优先 | 2026-07-18 |
| 9 | Plan ⇄ Clarify 外循环不限轮次，内循环 ≤5 轮 | 2026-07-18 |
| 10 | ReAct 执行放 Celery Worker（max 8 rounds） | 2026-07-18 |
| 11 | Ops 无 plan_review，直接执行 | 2026-07-18 |
| 12 | 6 个 doc_* 工具，仅绑定文档 Session 可用 | 2026-07-18 |
| 13 | idempotency: correlation_id/task_id/msg_id 三级去重 | 2026-07-18 |
| 14 | busy 状态按 node 区分超时（2min/5min/10min） | 2026-07-18 |

---

## 12. 相关文档

| 文档 | 状态 |
|------|:---:|
| 开发计划 `development-plan.md` | ✅ |
| API 参考 `api-reference.md` | 待更新 |
| WS 协议 `websocket-protocol.md` | 待更新 |
| 数据库架构 `database-architecture.md` | 待更新 |
| 验收标准 `acceptance-criteria.md` | 待更新 |
| 反幻觉 `anti-hallucination.md` | ✅ |
| 记忆系统 `memory-system.md` | ✅ |
