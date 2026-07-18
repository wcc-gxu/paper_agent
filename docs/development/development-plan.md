# Paper Agent v4.0 — 后端开发计划

> 日期: 2026-07-18 | 目标: 2026-08-15
> 对应前端: [paper-agent-vue v4.0](../../paper-agent-vue/docs/development-plan.md)

---

## 一、架构变更速览

```
v3.1                          v4.0
───────────────────────────────────────────────────
fast_triage(flash)             intent_classify(flash, 7意图独立打分)
  → chat/ops/research            → chat(flash reply) | ops(ops_plan→Celery) | research(plan⇄clarify)
execute 在 daemon 同步阻塞    全部 ReAct 拆分到 Celery Worker
无 Agent 生命周期管理          Redis 心跳 + 独立 Task + API 启停
无文档系统                     MD 文档 CRUD + 版本历史 + OSS 存储
无用户偏好                     研究领域/写作风格/语言/导师语录
知识库无隔离                   按 user_id 自动过滤 + 细粒度共享
注册返回 agent_id              不再返回，后台自动创建 Agent
```

---

## 二、阶段划分

| 阶段 | 范围 | 估时 | 依赖 |
|------|------|------|------|
| P0 | Docker 重置 + DB 迁移 | 0.5d | 无 |
| P1 | API: Agent / Document / Preference / Share | 3d | P0 |
| P2 | Agent 心跳 + Celery ReAct 拆分 | 4d | P1 |
| P3 | Intent Classify + Plan ⇄ Clarify 循环 | 3d | P2 |
| P4 | OSS 文档存储 + 知识库用户隔离 | 1d | P0 |
| P5 | WS 协议 v11.0 适配 | 1d | P2 |
| P6 | 联调前端 + 验收 | 2d | P3+P5 |
| **总计** | | **~14.5d** | |

---

## 三、P0 — 数据库迁移

### 新建表

```sql
-- agents
CREATE TABLE agents (
  id VARCHAR(64) PRIMARY KEY DEFAULT gen_agent_id(),
  user_id VARCHAR(64) UNIQUE NOT NULL REFERENCES users(id),
  system_prompt TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- documents
CREATE TABLE documents (
  id VARCHAR(64) PRIMARY KEY DEFAULT gen_doc_id(),
  user_id VARCHAR(64) NOT NULL REFERENCES users(id),
  title VARCHAR(200) NOT NULL,
  file_path VARCHAR(500) NOT NULL,
  is_auto_review BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- document_versions
CREATE TABLE document_versions (
  id VARCHAR(64) PRIMARY KEY DEFAULT gen_ver_id(),
  document_id VARCHAR(64) NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_number INT NOT NULL,
  content TEXT NOT NULL,
  trigger VARCHAR(32) CHECK (trigger IN ('manual_commit','ai_turn','auto_save','rollback')),
  session_id VARCHAR(64),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- user_preferences
CREATE TABLE user_preferences (
  user_id VARCHAR(64) PRIMARY KEY REFERENCES users(id),
  research_domain VARCHAR(200) DEFAULT '',
  writing_style VARCHAR(100) DEFAULT 'APA',
  language_pref VARCHAR(50) DEFAULT 'zh',
  mentor_quotes TEXT DEFAULT '',
  other JSONB DEFAULT '{}',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- share_requests
CREATE TABLE share_requests (
  id VARCHAR(64) PRIMARY KEY DEFAULT gen_share_id(),
  from_user_id VARCHAR(64) NOT NULL REFERENCES users(id),
  to_user_id VARCHAR(64) NOT NULL REFERENCES users(id),
  resource_type VARCHAR(32) NOT NULL,
  resource_id VARCHAR(64) NOT NULL,
  message TEXT DEFAULT '',
  status VARCHAR(16) DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 修改现有表

```sql
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS document_id VARCHAR(64) REFERENCES documents(id);
ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS user_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_user_id ON knowledge_chunks(user_id);
```

### Docker 重置

```bash
# 完全清空
docker compose down -v
# 重新初始化
docker compose up -d
```

---

## 四、P1 — API 新增端点

### Agent 端点（新增 `api/routes.py` 路由）

| 端点 | 方法 | 实现文件 | 说明 |
|------|------|---------|------|
| `/api/agents/me` | GET | routes.py | 查询 agents 表 + Redis 心跳合并 |
| `/api/agents/me` | PUT | routes.py | 更新 system_prompt |
| `/api/agents/me/status` | GET | routes.py | 只读 Redis `agent:heartbeat:{user_id}` |
| `/api/agents/me/start` | POST | routes.py | 触发 Docker API 或 subprocess 启动 daemon |
| `/api/agents/me/stop` | POST | routes.py | 向 daemon 发停止信号 + DEL heartbeat key |

### Document 端点

| 端点 | 方法 | 实现要点 |
|------|------|---------|
| `/api/documents` | GET/POST | 列出/创建文档（支持 mode=create/upload/from_paper） |
| `/api/documents/{id}` | GET/PUT/DELETE | CRUD + 乐观锁（version 匹配） |
| `/api/documents/{id}/download` | GET | 返回 `Content-Disposition: attachment` |
| `/api/documents/{id}/versions` | GET/POST | 版本列表/手动提交 |
| `/api/documents/{id}/versions/{vid}` | GET | 版本内容 |
| `/api/documents/{id}/revert/{vid}` | POST | 回滚+创建新版本 |

### Preference / Share 端点

| 端点 | 说明 |
|------|------|
| `GET/PUT /api/preferences/me` | 用户偏好 CRUD，部分更新 |
| `POST /api/share` | 发起共享请求 |
| `GET /api/share/requests` | 列出共享请求 |
| `PUT /api/share/requests/{id}` | 接受/拒绝 |

### 注册接口改动

| 改动 | 说明 |
|------|------|
| `POST /api/auth/register` | Response 移除 `agent_id`；注册后调用 `_create_agent_async()` 后台创建 Agent |

---

## 五、P2 — Agent 心跳 + Celery 拆分

### Heartbeat (daemon.py)

在 `AgentManager.__init__` 中新增独立心跳 Task:

```python
# daemon.py
async def _heartbeat_loop(self):
    r = aioredis.from_url(self.redis_url, decode_responses=True)
    key = f"agent:heartbeat:{self.user_id}"
    while not self._stopping:
        await r.set(key, json.dumps({
            "status": "running",
            "active_turns": self._active_turn_count,
            "current_session": self._current_session_id,
        }), ex=15)
        await asyncio.sleep(10)
```

在 `run()` 启动时: `asyncio.create_task(self._heartbeat_loop(r))`

### Heartbeat 检测 (app.py)

消息入队 (`_push_to_redis`) 前检测:

```python
heartbeat = await redis.get(f"agent:heartbeat:{user_id}")
if not heartbeat:
    await ws.send_text(error_AGENT_NOT_RUNNING())
    return
await redis.lpush(queue, msg)
if heartbeat["active_turns"] > 0:
    await ws.send_text(status_queued(heartbeat["active_turns"]))
```

### Celery ReAct 拆分

**拆分目标**: `MainAgent._run_turn()` 的 execute 阶段 → Celery Task

**新建 `agent/react_executor.py`**:

```python
@celery_app.task(bind=True, max_retries=3, acks_late=True)
def react_execute(self, plan, agent_id, user_id, session_id, context):
    round = 0
    while round < 8:
        result = llm.decide(context + tool_results)
        if result.done:
            outbox_publish(message_reply(summary=result.summary))
            return
        results = execute_parallel(result.actions)
        context.add(results)
        round += 1
    outbox_publish(message_reply(best_effort_summary))
```

**daemon 修改**: plan 批准后 `celery_app.send_task("react_execute", ...)` → daemon 立即回到 BRPOP

**取消支持**: `celery_app.control.revoke(task_id, terminate=True)` → 捕获 `WorkerLostError`/`Terminated` → outbox_publish(status="cancelled")

### 文件改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `agent/daemon.py` | 修改 | 新增 `_heartbeat_loop`, 修改 `run()` 启动 heartbeat |
| `api/app.py` | 修改 | `_push_to_redis` 前检测 heartbeat |
| `agent/react_executor.py` | **新建** | Celery ReAct execute task |
| `agent/main_agent.py` | 修改 | `_run_turn` 拆分：plan→review→submit celery |
| `agent/celery_app.py` | 修改 | 注册新 task |

---

## 六、P3 — Intent Classify + Plan ⇄ Clarify 循环

### intent_classify 实现

**文件**: `agent/main_agent.py` 新增 `_node_intent_classify()` 替换 `_node_fast_triage`

- Flash model (DeepSeek Flash) + `tool_choice` 强制结构化 JSON
- 7 种意图独立打分，score > 0.7 → intents[]
- 输出 `planning_prompt`（规范化意图描述）
- `should_plan = false` → 直接 `_node_inline_reply`

**Prompt 文件**: `agent/main_agent_prompts.py` 新增 `INTENT_CLASSIFY_PROMPT`

### plan ⇄ clarify 循环

**daemon.py** 新增 `plan_clarify_loop()`:

```python
async def plan_clarify_loop(self, planning_prompt, session, preferences):
    recent_events = []
    while True:
        result = await self._node_plan(planning_prompt, recent_events)
        if result.needs_clarification:
            info = await self._node_clarify(result.goal, result.suggested_tools)
            recent_events.append(info.collected_info)
            continue
        approved, feedback = await self._node_plan_review(result.plan)
        if approved:
            return result.plan
        recent_events.append(feedback)
```

**clarify_node**: ReAct 循环 ≤5 轮。LLM decide → tool_call → evaluate → ... → 返回 `collected_info` 摘要

### 文件改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `agent/main_agent_prompts.py` | 修改 | 新增 INTENT_CLASSIFY_PROMPT + PLAN_PROMPT + CLARIFY_PROMPT |
| `agent/main_agent.py` | 修改 | 新增 `_node_intent_classify`, 重构 `_node_plan`, 新增 `_node_clarify` |
| `agent/daemon.py` | 修改 | 新增 `plan_clarify_loop()` |
| `agent/graphs/main_graph.py` | 修改 | 更新 StateGraph：简化节点 |

---

## 七、P4 — OSS + 知识库隔离

### OSS 文档存储

- 目录: `oss/documents/{user_id}/` 和 `oss/documents/{user_id}/reviews/`
- PDF 目录: `oss/papers/{user_id}/` (按用户划分)
- `documents` 表 `file_path` 记录相对路径

### 知识库 user_id 过滤

- 所有 knowledge 端点在 pgvector 查询时加 `WHERE user_id = JWT.user_id`
- 上传/搜索入库时写入 `user_id`

---

## 八、P5 — WS 协议 v11.0

### 新增消息类型 (outbox.py)

| type | subType | 说明 |
|------|---------|------|
| `error` | `AGENT_NOT_RUNNING` | Agent 心跳不存在时由 app.py 直接推送 |
| `status` | `stage: "queued"` | 消息入队但 Agent 正忙 |
| `tool` | `call` (doc_*) | 文档编辑工具 |
| — | — | DiffPreview 卡片（前端渲染，非 WS 新 type） |

### 新增 capability (ws.py)

```
document_edit → doc_read, doc_write_section, doc_append, doc_diff_apply, doc_generate_review, doc_search_rag
```

### 文件改动

| 文件 | 改动 |
|------|------|
| `agent/outbox.py` | 新增 `error/AGENT_NOT_RUNNING` 输出支持 |
| `api/ws.py` | 新增 `document_edit` capability 注册 |
| `api/app.py` | 心跳检测 → AGENT_NOT_RUNNING 推送 |

---

## 九、P6 — 联调与验收

### 联调清单

- [ ] 注册 → Agent 自动创建 → 轮询启动成功
- [ ] WS 消息 → heartbeat 检测 → AGENT_NOT_RUNNING 正确处理
- [ ] 写作 Session → WS sync 含 document_edit
- [ ] AI `doc_diff_apply` → Client 返回 tool_result(done/failed)
- [ ] Celery execute → outbox_publish 正常推送 → WS 客户端收到
- [ ] Celery 任务取消 → outbox status cancelled
- [ ] Documents CRUD → OSS 文件验证
- [ ] Knowledge 查询按 user_id 隔离（用户 A 看不到用户 B 的数据）
- [ ] Plan ⇄ Clarify 循环 → 多次澄清后产出 plan

---

## 十、风险与依赖

| 风险 | 级别 | 缓解 |
|------|:---:|------|
| Celery ReAct 拆分影响现有 turn 流程 | 高 | 先在 celery worker 复用 main_agent ReAct 逻辑 |
| Heartbeat 独立 Task 与 BRPOP 并发安全 | 中 | asyncio.Task 天然隔离，共享状态用原子操作 |
| Plan ⇄ Clarify 无限循环 | 低 | clarify 内部 ≤5 轮 react 限制 |
| OSS 文件迁移（旧 PDF 无 user_id） | 中 | 迁移脚本按现有用户分配 |

---

## 十一、环境变量（新增）

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENT_HEARTBEAT_TTL` | `15` | Redis Agent 心跳 TTL（秒） |
| `AGENT_IDLE_TIMEOUT` | `1800` | 空闲自动停止超时（秒） |
| `MAX_CLARIFY_ROUNDS` | `5` | Clarify ReAct 最大轮次 |
| `REACT_MAX_ROUNDS` | `8` | Celery Execute ReAct 最大轮次 |
