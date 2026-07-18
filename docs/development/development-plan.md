# Paper Agent v4.0 — 后端开发计划

> 日期: 2026-07-18 | 目标: 2026-08-15
> 对应前端: [paper-agent-vue v4.0](../../paper-agent-vue/docs/development-plan.md)
> 基于当前代码基线（commit `cd0b94f`）与 v4.0 文档的差异分析

---

## 零、现状与差距总览

### 已有（无需重新开发）

| 能力 | 文件 | 备注 |
|------|------|------|
| 10 节点 MainGraph | `agent/graphs/main_graph.py` | fast_triage→intent_classify→plan→gate→execute→evaluate |
| `agents` 表 + CRUD API | `scripts/init_db.sql:125` + `api/routes.py:141` | 现有 schema 更丰富（name/display_name/agent_type/llm_provider/config） |
| AgentManager 多用户路由 | `agent/daemon.py:99` | 已支持 per-user agent 加载 |
| PostgreSQL + pgvector | Docker compose + `scripts/init_db.sql` | 22+ 业务表 + 4 向量表 |
| Celery Worker/Beat | `agent/celery_app.py` + `docker-compose.yml` | 已部署 |
| WS 协议 v10 | `agent/outbox.py` + `api/ws.py` | status/tool/ask/gate 消息已实现 |
| Plan ⇄ Clarify 内循环 | `main_graph.py:_clarify()` | MAX_CLARIFY_ROUNDS=3 |
| JWT 认证 | `api/auth.py` | register/login/refresh/me |
| 注册自动创建 Agent | `api/auth.py` | 已实现（commit `3232062`） |

### 与 v4.0 文档的差距

| 文档描述 | 实际状态 | 差距 |
|----------|----------|------|
| 7 意图独立打分 intent_classify | 3 维 pass-through (FastTriageV31) | **需重写** |
| intent_classify 用 Flash + tool_choice | Flash 已用，无 tool_choice 强制 JSON | **需实现** |
| ReAct 全量放 Celery Worker | ReAct 内联在 main_graph.py._execute() | **需拆分** |
| Agent 心跳 Redis key | 不存在 | **需新建** |
| heartbeat 检测 → AGENT_NOT_RUNNING | app.py 不检测 | **需实现** |
| documents / document_versions 表 | 不在 init_db.sql | **需建表** |
| 文档 CRUD API + 版本管理 | 无路由 | **需新建** |
| user_preferences (PG v4 schema) | 旧 SQLite 版在 memory.py | **需迁移** |
| 知识库 user_id 隔离 | paper_chunks 已有 user_id，但部分查询未过滤 | **需补全** |
| OSS 按 user_id 分目录 | 未实现 | **需实现** |
| share_requests 表 + API | 不存在 | **需新建** |
| WS v11: AGENT_NOT_RUNNING/queued/doc_* | 不存在 | **需实现** |
| sessions.document_id 列 | 不存在 | **需 ALTER** |

---

## 一、架构变更速览

```
当前 (commit cd0b94f)           v4.0 目标
───────────────────────────────────────────────────
intent_classify (3维 pass-through)  intent_classify (7意图独立打分 + Flash tool_choice)
ReAct 内联 main_graph.py          ReAct 独立 celery task (react_executor.py)
无 Agent 生命周期管理              Redis 心跳 + 独立 Task + API 启停
无文档系统                         MD 文档 CRUD + 版本历史 + OSS 存储
偏好存 SQLite (memory.py)         偏好存 PostgreSQL (user_preferences 表)
知识库部分隔离                     全量 user_id 过滤 + 细粒度共享
注册返回 agent_id                  已移除（commit 3232062 完成）
agents 表 (多字段富 schema)       agents 表保持现有 schema，增加 status 查询
```

---

## 二、阶段划分

| 阶段 | 范围 | 估时 | 依赖 |
|------|------|------|------|
| P0 | DB 迁移: documents/document_versions/user_preferences/share_requests + sessions.document_id | 0.5d | 无 |
| P1 | API: 文档 CRUD / 偏好 PG / Agent 状态 / 共享 | 3d | P0 |
| P2 | Agent 心跳 + Celery ReAct 拆分 | 4d | P1 |
| P3 | 7 意图 Intent Classify + Plan ⇄ Clarify 外循环 | 3d | P2 |
| P4 | OSS 文档存储 + 知识库全量隔离 | 1d | P0 |
| P5 | WS 协议 v11.0 适配 | 1d | P2 |
| P6 | 联调前端 + 验收 | 2d | P3+P5 |
| **总计** | | **~14.5d** | |

---

## 三、P0 — 数据库迁移（0.5d）

### 3.1 新建表

所有 `gen_*_id()` 函数需要在 `init_db.sql` 中新增定义。

```sql
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
CREATE INDEX idx_documents_user ON documents(user_id);

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
CREATE INDEX idx_versions_doc ON document_versions(document_id);

-- user_preferences (PG v4 schema)
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
  status VARCHAR(16) DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 3.2 修改现有表

```sql
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS document_id VARCHAR(64) REFERENCES documents(id);
```

> `paper_chunks` 已有 `user_id` 列，无需 ALTER。

### 3.3 ID 生成函数

```sql
CREATE OR REPLACE FUNCTION gen_doc_id() RETURNS VARCHAR(64) AS $$
BEGIN RETURN 'doc-' || LOWER(REPLACE(gen_random_uuid()::TEXT, '-', ''));
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION gen_ver_id() RETURNS VARCHAR(64) AS $$
BEGIN RETURN 'ver-' || LOWER(REPLACE(gen_random_uuid()::TEXT, '-', ''));
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION gen_share_id() RETURNS VARCHAR(64) AS $$
BEGIN RETURN 'shr-' || LOWER(REPLACE(gen_random_uuid()::TEXT, '-', ''));
END; $$ LANGUAGE plpgsql;
```

### 3.4 Docker 重置

```bash
docker compose down -v
docker compose up -d
# 然后运行 init_db.sql
```

### 3.5 文件改动

| 文件 | 操作 |
|------|------|
| `scripts/init_db.sql` | 追加 CREATE TABLE documents/document_versions/user_preferences/share_requests + gen_*_id 函数 + ALTER sessions |

---

## 四、P1 — API 新增端点（3d）

### 4.1 Agent 端点（修改现有 routes.py）

> 现有 agents CRUD 保留不变。新增面向当前用户的便捷端点。

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/agents/me` | GET | 从 JWT 提取 user_id → 查 agents 表（取第一条活跃 agent）+ Redis 心跳合并 status |
| `/api/agents/me` | PUT | 更新 system_prompt（user_id 关联的 agent） |
| `/api/agents/me/status` | GET | 只读 Redis `agent:heartbeat:{user_id}`，返回 `{status, active_turns, progress}` |
| `/api/agents/me/start` | POST | 异步启动 daemon（Docker API 或 subprocess） → 轮询 status 确认 |
| `/api/agents/me/stop` | POST | 发停止信号 + DEL heartbeat key → 轮询确认 |

### 4.2 Document 端点（新文件 `api/document_routes.py` 或追加到 routes.py）

| 端点 | 方法 | 实现要点 |
|------|------|---------|
| `/api/documents` | GET | 列出当前用户文档（支持 `?search=` 模糊标题搜索） |
| `/api/documents` | POST | 创建文档。Body: `{title, content?, mode}` — mode=create(新空白) / upload(MD文件) / from_paper(paper_id→转MD) |
| `/api/documents/{id}` | GET | 返回文档 + 当前版本内容 |
| `/api/documents/{id}` | PUT | 更新（乐观锁: body 带 `version` 字段，与 DB 当前版本比对） |
| `/api/documents/{id}` | DELETE | 软删除或真删除 |
| `/api/documents/{id}/download` | GET | 返回 MD 文件 `Content-Disposition: attachment` |
| `/api/documents/{id}/versions` | GET | 版本列表（按 version_number DESC） |
| `/api/documents/{id}/versions` | POST | 手动提交新版本（trigger=manual_commit） |
| `/api/documents/{id}/versions/{vid}` | GET | 版本内容 |
| `/api/documents/{id}/revert/{vid}` | POST | 回滚 → 创建新版本（trigger=rollback，content=vid 版本内容） |

### 4.3 Preference 端点

| 端点 | 说明 |
|------|------|
| `GET /api/preferences/me` | 查 user_preferences 表。merge 旧 SQLite 数据（如有），初始化默认行 |
| `PUT /api/preferences/me` | 部分更新（PATCH 语义，只更新传入字段） |

### 4.4 Share 端点

| 端点 | 说明 |
|------|------|
| `POST /api/share` | Body: `{to_user_id, resource_type, resource_id, message?}` → 插入 share_requests |
| `GET /api/share/requests` | 列出收到/发出的请求（`?direction=inbound\|outbound`） |
| `PUT /api/share/requests/{id}` | Body: `{status: "accepted"\|"rejected"}` |

### 4.5 注册接口改动

| 改动 | 说明 |
|------|------|
| `POST /api/auth/register` | 已移除 agent_id 返回（commit `3232062` 完成）。无需改动 |

### 4.6 文件改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `api/routes.py` | 修改 | 新增 /agents/me/*, /documents/*, /preferences/me, /share/* |
| `agent/pgdb.py` | 修改 | 新增 documents/document_versions/share_requests/user_preferences DAO 方法 |
| `api/auth.py` | 无需改动 | 已实现 |

---

## 五、P2 — Agent 心跳 + Celery ReAct 拆分（4d）

### 5.1 Heartbeat (daemon.py)

在 `AgentManager.__init__` 中新增独立心跳 Task:

```python
# daemon.py — AgentManager 中新增
async def _heartbeat_loop(self, redis_client):
    key = f"agent:heartbeat:{self.user_id}"
    while not self._stopping:
        await redis_client.set(key, json.dumps({
            "status": "running",
            "active_turns": self._active_turn_count,
            "current_session": self._current_session_id,
        }), ex=15)
        await asyncio.sleep(10)
```

在 `AgentManager.run()` 启动时:
```python
self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(redis))
```

### 5.2 Heartbeat 检测 (app.py)

消息入队 (`_push_to_redis`) 前检测:

```python
# api/app.py — 在 WebSocket message handler 中
heartbeat_raw = await redis.get(f"agent:heartbeat:{user_id}")
if not heartbeat_raw:
    await ws.send_text(json.dumps({
        "type": "error", "subType": "AGENT_NOT_RUNNING",
        "payload": {"code": "AGENT_NOT_RUNNING", "message": "Agent 未运行，请先启动"}
    }))
    return

heartbeat = json.loads(heartbeat_raw)
await redis.lpush(f"agent:ws:{user_id}", serialized_msg)

if heartbeat.get("active_turns", 0) > 0:
    await ws.send_text(json.dumps({
        "type": "status", "payload": {"stage": "queued", "message": f"前面有 {heartbeat['active_turns']} 个任务排队中"}
    }))
```

### 5.3 Celery ReAct 拆分

**目标**: `main_graph.py._execute()` 的 ReAct 逻辑 → 独立 Celery Task

**新建 `agent/react_executor.py`**:

```python
# agent/react_executor.py
@celery_app.task(bind=True, max_retries=3, acks_late=True)
def react_execute(self, plan: dict):
    """
    args = {
        "plan_id", "agent_id", "user_id", "session_id",
        "todos": [...],  # approved_todos
        "context": {"document_id": ..., "preferences": ...}
    }
    """
    round_num = 0
    context = build_initial_context(plan)
    while round_num < REACT_MAX_ROUNDS:  # default 8
        result = llm_pro_client.decide(context + tool_results)
        if result.done:
            outbox_publish_sync("message", "reply", user_id, agent_id, session_id,
                              payload={"content": result.summary})
            return {"status": "done", "summary": result.summary}

        results = execute_tools_parallel(result.tool_calls)
        for r in results:
            outbox_publish_sync("tool_execution", None, user_id, agent_id, session_id,
                              payload=r.to_dict())
        context.extend(results)
        round_num += 1

    outbox_publish_sync("message", "reply", user_id, agent_id, session_id,
                      payload={"content": best_effort_summary(context)})
```

**daemon 修改**: `_gate()` 中 plan_approve 后:
```python
task = celery_app.send_task("react_execute", args=[plan_dict])
# daemon 立即回到 BRPOP，不等待结果
```

**取消支持**:
```python
# 用户发送 cancel 消息 → daemon 收到
celery_app.control.revoke(task_id, terminate=True)
# Worker 捕获 RevokedError → outbox_publish(status="cancelled")
```

### 5.4 文件改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `agent/daemon.py` | 修改 | 新增 `_heartbeat_loop`，修改 `run()` 启动 heartbeat + plan_approve 后 submit Celery |
| `api/app.py` | 修改 | `_push_to_redis` 前检测 heartbeat |
| `agent/react_executor.py` | **新建** | Celery ReAct execute task |
| `agent/main_graph.py` | 修改 | `_execute()` 逻辑提取可复用部分供 react_executor 调用 |
| `agent/graphs/main_graph.py` | 修改 | `_gate()` plan_approve → submit Celery 而非内联 execute |
| `agent/celery_app.py` | 修改 | 注册 `react_execute` task |

---

## 六、P3 — Intent Classify + Plan ⇄ Clarify 外循环（3d）

### 6.1 intent_classify 实现

**文件**: `agent/graphs/main_graph.py` — 重写 `_intent_classify()`

当前状态:
- `_intent_classify()` 是 `_fast_triage()` 的 pass-through（复用 FastTriageV31Result）
- 需要改为 7 意图独立打分 + `planning_prompt` 生成 + Flash `tool_choice` 强制 JSON

目标实现:
```python
async def _intent_classify(self, state: MainState) -> dict:
    result = await self._llm_flash.chat_json(
        messages=[{"role": "system", "content": INTENT_CLASSIFY_PROMPT},
                  {"role": "user", "content": state["user_input"]}],
        force_tool=True,  # 强制 tool_choice 输出结构化 JSON
        tool_name="classify_intent",
        tool_schema={
            "intents": [{"intent": "survey|kb_retrieval|paper_analysis|writing|knowledge_mgmt|chat|ops",
                         "score": 0.0}],
            "planning_prompt": "...",
            "complexity": "simple|medium|complex",
            "should_plan": True,
            "hint": "..."
        }
    )
    # 过滤 score > 0.7
    intents = [i for i in result["intents"] if i["score"] > 0.7]

    if not result["should_plan"]:
        return {"route": "chat_reply", "intents": intents}

    if all(i["intent"] == "ops" for i in intents):
        return {"route": "ops_plan", "intents": intents}

    return {"route": "plan", "intents": intents, "planning_prompt": result["planning_prompt"]}
```

**Prompt 文件**: `agent/main_agent_prompts.py` 新增 `INTENT_CLASSIFY_PROMPT`:
```python
INTENT_CLASSIFY_PROMPT = """你是意图分类器。对用户输入按以下 7 种意图独立打分（0-1）:
- survey: 文献调研，需要搜索论文
- kb_retrieval: 知识库检索/问答
- paper_analysis: 单篇论文精读分析
- writing: AI 辅助学术写作
- knowledge_mgmt: 知识管理（入库/订阅）
- chat: 闲聊/知识问答/文本处理（非学术）
- ops: 运维操作（系统管理）

规则:
1. 每种意图独立打分，互不影响
2. score > 0.7 的进入 intents[]
3. 生成 planning_prompt: 规范化意图描述（含用户目标、所需工具、约束、偏好）
4. should_plan=false 当且仅当所有 intent 都是 chat
5. 仅 chat → 直接 flash reply；仅 ops → ops_plan；含 research → 走 plan
"""
```

### 6.2 Plan ⇄ Clarify 外循环

**daemon.py** 新增 `plan_clarify_loop()`:

当前状态:
- `_clarify()` 在 main_graph.py 中，是 Graph 内部节点（MAX_CLARIFY_ROUNDS=3）
- 需要提升到 daemon 层面控制外循环（不限轮次），内循环（clarify_node）限制 ≤5 轮

目标实现:
```python
# daemon.py — AgentManager 中新增
async def plan_clarify_loop(self, planning_prompt: str,
                            session_id: str, preferences: dict):
    recent_events = []
    max_outer_rounds = 5  # 外循环安全上限
    for _ in range(max_outer_rounds):
        # 调用 plan_node
        result = await self._invoke_plan_node(
            planning_prompt=planning_prompt,
            recent_events=recent_events,
            preferences=preferences,
            session_id=session_id,
        )

        if result.get("needs_clarification"):
            # 内循环: clarify_node (ReAct ≤5 轮)
            info = await self._invoke_clarify_node(
                goal=result["clarification_goal"],
                suggested_tools=result.get("suggested_tools", []),
                max_rounds=5,
            )
            recent_events.append({"collected_info": info["collected_info"]})
            continue  # 回到 plan_node

        # 不需要澄清 → plan_review
        approved, feedback = await self._invoke_gate(result["plan"])
        if approved:
            return result["plan"]  # → Celery react_execute
        recent_events.append({"feedback": feedback})

    # 超限 fallback: 返回当前最佳 plan
    return result.get("plan")
```

### 6.3 文件改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `agent/main_agent_prompts.py` | 修改 | 新增 `INTENT_CLASSIFY_PROMPT` |
| `agent/graphs/main_graph.py` | 修改 | 重写 `_intent_classify()` → 7 意图打分 + planning_prompt |
| `agent/daemon.py` | 修改 | 新增 `plan_clarify_loop()` 控制外循环 |
| `agent/main_agent.py` | 修改 | 接入新的 intent_classify 路由逻辑 |

---

## 七、P4 — OSS + 知识库隔离（1d）

### 7.1 OSS 文档存储

- 目录结构:
  ```
  oss/
  ├── documents/{user_id}/          # MD 文档
  │   └── reviews/                  # 自动生成的综述
  └── papers/{user_id}/             # PDF 论文（按用户划分）
  ```
- `documents.file_path` 记录相对路径（如 `alice/doc-xxx.md`）
- 上传/创建文档时写入 OSS 目录
- 已有 `papers.file_path` 迁移到新目录结构

### 7.2 知识库 user_id 全量隔离

- 所有 knowledge 查询在 pgvector 查询时强制 `WHERE user_id = JWT.user_id`
- 检查 `paper_chunks` 已有 `user_id` 列 → 确认所有 DAO 方法都带 user_id 过滤
- `api/routes.py` 中 knowledge 端点：从 JWT 提取 user_id，传给 DAO
- 上传/搜索入库时写入 `user_id`

### 7.3 文件改动

| 文件 | 操作 | 说明 |
|------|------|------|
| `agent/pgvector_store.py` | 检查 | 确认所有查询含 user_id 过滤 |
| `agent/pgdb.py` | 修改 | 新增 documents CRUD DAO |
| `api/routes.py` | 修改 | knowledge 端点传入 user_id |
| `scripts/init_db.sql` | 无需改动 | paper_chunks 已有 user_id |

---

## 八、P5 — WS 协议 v11.0（1d）

### 8.1 新增消息类型

| type | subType | 说明 | 触发位置 |
|------|---------|------|---------|
| `error` | `AGENT_NOT_RUNNING` | Agent 心跳不存在时由 app.py 直接推送 | `api/app.py` |
| `status` | `stage: "queued"` | 消息入队但 Agent 正忙（active_turns > 0） | `api/app.py` |
| `tool` | `call` (doc_*) | 文档编辑工具调用 | `outbox.py` + `main_graph.py` |

### 8.2 新增 WS capability

```python
# api/ws.py — 已有 capabilities 机制，只需增加
"document_edit": ["doc_read", "doc_write_section", "doc_append",
                  "doc_diff_apply", "doc_generate_review", "doc_search_rag"]
```

### 8.3 文件改动

| 文件 | 改动 |
|------|------|
| `agent/outbox.py` | 新增 `error/AGENT_NOT_RUNNING` → `_build_error_envelope()` |
| `api/ws.py` | 新增 `document_edit` capability 注册 |
| `api/app.py` | WebSocket message handler 中 heartbeat 检测 + AGENT_NOT_RUNNING/queued 推送 |
| `agent/graphs/main_graph.py` | `_dispatch_tool()` 中注册 doc_* 6 个工具 |

---

## 九、P6 — 联调与验收（2d）

### 9.1 联调清单

- [ ] 注册 → Agent 自动创建 → 轮询 `GET /api/agents/me/status` 直到 running
- [ ] WS 消息 → heartbeat 检测 → AGENT_NOT_RUNNING 正确处理（前端展示错误提示）
- [ ] Agent busy (active_turns > 0) → 消息入队 → status{stage:"queued"} 推送
- [ ] 写作 Session → WS sync 含 document_edit capability
- [ ] AI `doc_diff_apply` → Client 返回 tool_result(done/failed)
- [ ] Celery react_execute → outbox_publish 正常推送 → WS 客户端收到 message/reply
- [ ] Celery 任务取消 → outbox status cancelled
- [ ] Documents CRUD (GET/POST/PUT/DELETE) → OSS 文件验证存在
- [ ] Documents 版本管理 (versions/revert) → 版本号递增 + trigger 正确
- [ ] Knowledge 查询按 user_id 隔离（用户 A 看不到用户 B 的 paper_chunks）
- [ ] Preferences GET/PUT → 写入 PostgreSQL user_preferences 表
- [ ] Plan ⇄ Clarify 外循环 → 多次澄清后产出 plan

### 9.2 验收项数

| 级别 | 数量 | 内容 |
|:---:|:---:|------|
| P0 | 17 | Agent 生命周期、消息路由、Celery 执行、Intent Classify、文档 CRUD、知识库隔离 |
| P1 | 6 | queued 状态、Celery 取消、版本管理完整流程、Plan ⇄ Clarify 多轮 |
| P2 | 2 | Share 流程 |

详见 [acceptance-criteria.md](./acceptance-criteria.md)（25 项验收清单）。

---

## 十、风险与依赖

| 风险 | 级别 | 缓解 |
|------|:---:|------|
| Celery ReAct 拆分影响现有 turn 流程 | **高** | 先在 celery worker 复用 main_graph ReAct 逻辑；保持 _execute() 降级路径（Celery 不可用时回退内联执行） |
| Heartbeat 独立 Task 与 BRPOP 并发安全 | 中 | asyncio.Task 天然隔离，共享状态用 `self._active_turn_count`（原子 int） |
| Plan ⇄ Clarify 外循环无限 | 低 | 外循环 max_outer_rounds=5 + 内循环 ≤5 轮双层限制 |
| OSS 旧 PDF 无 user_id 目录 | 中 | 迁移脚本按现有 user_id 移动文件 |
| `agents` 表 schema 与文档不完全一致 | 低 | 现有 schema 是超集（多了 name/display_name/agent_type/is_active 等字段），不影响 v4 功能 |

---

## 十一、环境变量（新增）

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENT_HEARTBEAT_TTL` | `15` | Redis Agent 心跳 TTL（秒） |
| `AGENT_IDLE_TIMEOUT` | `1800` | 空闲自动停止超时（秒） |
| `MAX_CLARIFY_ROUNDS` | `5` | Clarify ReAct 内循环最大轮次 |
| `REACT_MAX_ROUNDS` | `8` | Celery Execute ReAct 最大轮次 |
| `MAX_PLAN_CLARIFY_OUTER` | `5` | Plan ⇄ Clarify 外循环最大轮次 |
| `OSS_BASE_PATH` | `./oss` | OSS 文件存储根目录 |

---

## 十二、与原计划的差异说明

| 差异项 | 原计划 | 更新后 | 原因 |
|--------|--------|--------|------|
| agents 表 | 新建 `agents(id, user_id, system_prompt)` | 使用现有 `agents` 表（含更多字段） | 现有 schema 已是超集，无需重建 |
| 注册不返回 agent_id | 待实现 | 已完成（commit `3232062`） | 已提前实现 |
| AgentManager | 待实现 | 已完成（commit `b3ab8d5`） | daemon.py 已有完整多用户路由 |
| Plan ⇄ Clarify | 完全新建 | 增强现有 clarify_node | main_graph.py 已有 3 轮内循环，提为外循环 |
| 知识库隔离 | 新建 user_id 列 | 补全查询过滤 | paper_chunks 已有 user_id，需补全 DAO 查询 |
| 文档编辑工具 | 6 个 doc_* 全部新建 | 注册 + WS 推送 | LLM 通过 tool_use 调用，后端做 dispatch |
