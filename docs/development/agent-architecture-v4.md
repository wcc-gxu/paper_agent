# Paper Agent v4 — Agent 架构设计文档

> 最后更新: 2026-07-18
> 状态: 最终设计（决策完成，进入开发）
> 来源: Claude Code 架构对话 + 12 轮需求确认

---

## 1. 概述

### 1.1 核心节点（v4.0 最终版）

```
Supervision Agent (编排层，在 daemon 中运行)
  │
  ├── intent_classify (flash)     — 意图分类（7种意图，独立打分，planning_prompt生成）
  │   ├── [chat] → flash reply → END
  │   ├── [ops] → ops_plan → Celery react execute → END
  │   └── [research] → plan_node ────────────────────── 循环 ───────
  │                       │                                          │
  │                       │  needs_clarification?                    │
  │                       │                                          │
  │                       ├── true → clarify_node                    │
  │                       │     (ReAct, max 5 rounds, tool+human)    │
  │                       │     → return collected_info              │
  │                       │     → 回到 plan_node ────────────────────┘
  │                       │
  │                       └── false → plan_review (Gate)
  │                              │
  │                        approve│ revise│
  │                          │     │       └→ 注入feedback → plan_node
  │                          ▼     ▼
  │                     Celery Worker
  │                     react_execute (ReAct, max 8 rounds)
  │                       │
  │                       ▼
  │                     message/reply → END
  │
  └── (独立心跳 Task) — 每 10s 刷新 Redis，与 turn 并行
```

### 1.2 关键设计变更

| 概念 | 旧草案 | 最终版 | 原因 |
|------|--------|--------|------|
| Agent 数量 | 每用户多个 Agent | 每用户 **1 个 Agent** | 科研场景下系统提示词统一 |
| 节点分类 | Actor/Plan/Judge/Responder/Gate 五型 | **intent_classify + plan_node + clarify_node + ops_plan** | LLM 做推理、代码做控制 |
| 执行分离 | execute 在 daemon 内 | **全部 ReAct 放 Celery** | daemon 立即释放，其他 session 可立即服务 |
| 审批 | 全走 plan_review | **ops 无 review，research 有 review** | ops 是管理员确定操作 |
| 心跳 | 无 | **独立 asyncio.Task**，10s 续期 | 解决长任务误判问题 |
| 规划节点 | 分离 | **一个 plan_node**，ops 有独立快速入口 | 统一工具注册，按角色过滤 |

### 1.3 7 种子意图

| 类型 | 子意图 | 工具范围 | 说明 |
|------|--------|---------|------|
| non-research | `chat` | None | 闲聊/知识问答/文本处理 → flash reply |
| non-research | `ops` | docker_*, system_* | 运维操作（admin only）→ ops_plan |
| research | `survey` | search, ingest | 文献调研 |
| research | `kb_retrieval` | kb_search, kb_ask | 知识库检索/问答 |
| research | `paper_analysis` | paper_read, kb_extract | 单篇论文精读 |
| research | `writing` | doc_* | AI 辅助写作 |
| research | `knowledge_mgmt` | ingest_url, subscription | 知识管理 |

---

## 2. 节点设计

### 2.1 节点总览

| 节点 | LLM | 所在进程 | 输出 | 说明 |
|------|-----|---------|------|------|
| `intent_classify` | Flash (≈2s) | daemon | `intents[]`, `planning_prompt`, `should_plan` | 意图分类+规范化提示词 |
| `chat_reply` | Flash/Pro | daemon | `message/reply` | 纯对话直接回答 |
| `ops_plan` | Flash (≈2s) | daemon | plan JSON → Celery | 运维规划，无 review 直接执行 |
| `plan_node` | Pro | daemon | `{ needs_clarification, plan? }` | 研究规划，按需请求澄清 |
| `clarify_node` | Pro | daemon | `collected_info` | ReAct 循环（≤5轮）+ 工具调用 + 用户交互 |
| `react_execute` | Pro | Celery Worker | `message/reply` | 执行已批准的计划（ReAct ≤8轮） |

### 2.2 intent_classify 节点

**System Prompt 核心规则**:
- 7 种意图独立打分 (0-1)，score > 0.7 的进入 `intents[]`
- 生成 `planning_prompt`：规范化意图描述，含用户目标、所需工具、约束、偏好
- `should_plan = false` → chat 直接回答
- Flash model 优先，confidence < 0.8 时升级 Pro 二次确认

**JSON Output**:
```json
{
  "intents": [{"intent": "survey", "score": 0.95}],
  "planning_prompt": "用户要求搜索 transformer 论文并生成综述...",
  "complexity": "medium",
  "should_plan": true,
  "hint": "可能需要1-2轮澄清"
}
```

### 2.3 plan_node

**输入**: `planning_prompt` + `recent_events`（clarify 返回的摘要）+ `user_preferences` + `session_context`

**输出**:
```json
// 需要澄清
{"needs_clarification": true, "clarification_goal": "...", "suggested_tools": [...], "suggested_questions": [...]}
// 计划就绪
{"needs_clarification": false, "plan": {"summary": "...", "danger_level": "medium", "todos": [...]}}
```

**约束**: plan_node 不调工具。需要工具时 → `needs_clarification = true` → daemon 路由到 clarify_node。

### 2.4 clarify_node

**ReAct 循环（≤5 rounds）**: LLM decide → tool_call → evaluate → ...

**可用工具**: 所有只读工具（web_search, kb_search, paper_read, doc_read）+ 用户交互（ask）

**用户交互**: `ask(choice/confirm/text)` → ws push → 等回复 → 注入 context

**返回**: `collected_info`（LLM 自摘要）+ `decisions`（已确认决策）→ 传给 plan_node

### 2.5 ops_plan

**触发**: intents 只含 `ops`。无 research 意图。

**行为**: Flash LLM → 产出 plan（含 batch tool calls）+ execution_strategy
**无 plan_review** → 直接 Celery 执行。工具权限按 `user.role` 过滤。

---

## 3. Plan ⇄ Clarify 循环（daemon 控制）

```python
async def plan_clarify_loop(planning_prompt, session, preferences):
    recent_events = []
    while True:
        result = await plan_node.invoke(planning_prompt + recent_events)
        
        if result.needs_clarification:
            info = await clarify_node.react_loop(
                goal=result.clarification_goal,
                max_rounds=5,  # clarify 内循环限制
            )
            recent_events.append({"collected_info": info.collected_info})
            continue
        
        approved, feedback = await gate.plan_review(result.plan)
        if approved:
            return result.plan  # → Celery
        recent_events.append({"feedback": feedback})
```

**Context 压缩**: 每轮 clarify→plan 返回时生成摘要，防止 `recent_events` 线性膨胀。

---

## 4. Celery ReAct Execute

**提交**:
```python
celery_app.send_task("react_execute", args=[{
    "plan_id", "agent_id", "user_id", "session_id",
    "todos": approved_todos,
    "context": {"document_id": ..., "preferences": ...}
}])
```

**Worker 执行**: while round < 8 → LLM decide → tool_execute (支持并行) → evaluate → done/summary

**取消**: `celery_app.control.revoke(task_id, terminate=True)` → 捕获 RevokedError → push status/cancelled

---

## 5. Agent 心跳

**daemon.py 心跳 Task**（独立，与 turn 并行）:
```python
async def _heartbeat_loop(self, r):
    key = f"agent:heartbeat:{self.user_id}"
    while not self._stopping:
        await r.set(key, json.dumps({"status": "running", "active_turns": N}), ex=15)
        await asyncio.sleep(10)
```

**app.py 检测**: 消息入队前 `GET agent:heartbeat:{user_id}` → 不存在则 `error/AGENT_NOT_RUNNING`；active_turns > 0 则 `status{stage:"queued"}`

---

## 6. 子 Agent 工具集

> 以下子 Agent 保持不变：Literature / Knowledge / Research / Writing / Translation / Glossary / Capture。由 `react_execute` 节点调用。

| Agent | 工具 | 说明 |
|-------|------|------|
| Literature | `agent_literature_search` | 搜索→下载→评估→排名 |
| Knowledge | `agent_knowledge_ingest/ask/ingest_local/get_fulltext` | RAG 入库/问答 |
| Research | `agent_clustering`, `agent_citation_chase` | 聚类+引用追踪 |
| Writing | `agent_generate_survey_v2`, `agent_check_ai_flavor` | 综述+AI味检查 |
| Translation | `agent_translate` | 学术翻译 |
| Glossary | `agent_build_glossary_v2` | 术语库构建 |
| Capture | `agent_capture_video` | 视频下载+转写 |

### v4.0 新增文档编辑工具

| 工具 | 条件 | 说明 |
|------|------|------|
| `doc_read` | Session 绑定文档 | 读取文档全文/部分 |
| `doc_write_section` | Session 绑定文档 | 覆盖段落 |
| `doc_append` | Session 绑定文档 | 追加内容 |
| `doc_diff_apply` | Session 绑定文档 | 生成 diff → DiffPreviewCard |
| `doc_generate_review` | Session 绑定文档 | 生成综述 → docs/reviews/ |
| `doc_search_rag` | Session 绑定文档 | 搜索知识库引用 |

---

## 7. 工具权限（按角色）

| 工具类别 | Student | Professor | Admin |
|---------|:---:|:---:|:---:|
| 论文搜索/RAG/上传/分析 | ✅ | ✅ | ✅ |
| 文档编辑 (doc_*) | ✅ | ✅ | ✅ |
| 写作/翻译/术语/订阅 | ✅ | ✅ | ✅ |
| Docker/系统操作 | ❌ | ❌ | ✅ |

---

## 8. 反幻觉体系

三层防线：人格设定 → 上下文质量（主战场） → 规则验证（兜底）。详见 [anti-hallucination.md](anti-hallucination.md)。

---

## 9. v4.0 决策记录

| # | 决策 | 日期 |
|---|------|------|
| 1 | 每用户 1 个 Agent（注册时自动创建） | 2026-07-18 |
| 2 | Redis 心跳 + 独立 Task + API 启停 | 2026-07-18 |
| 3 | intent_classify 用 Flash（confidence < 0.8 升 Pro） | 2026-07-18 |
| 4 | Plan ⇄ Clarify 外循环不限轮次，内循环 ≤5 轮 | 2026-07-18 |
| 5 | ReAct 全量放 Celery Worker | 2026-07-18 |
| 6 | Ops 无 plan_review，直接执行 | 2026-07-18 |
| 7 | Plan 修改后重新 plan_review | 2026-07-18 |
| 8 | Research+Ops 统一 plan_node（按角色过滤工具） | 2026-07-18 |
| 9 | 6 个 doc_* 工具，仅绑定文档 Session 可用 | 2026-07-18 |
| 10 | diff 以 DiffPreview 卡片呈现，用户接受/拒绝 | 2026-07-18 |
| 11 | 版本：手动+AI turn+自动快照 | 2026-07-18 |
| 12 | 用户偏好：4 项，界面编辑+对话修改 | 2026-07-18 |

---

## 10. 相关文档

| 文档 | 状态 |
|------|:---:|
| 后端开发计划 `development-plan.md` | ✅ |
| API 参考 `api-reference.md` | 待更新 |
| WS 协议 `websocket-protocol.md` | 待更新 |
| 数据库架构 `database-architecture.md` | 待更新 |
| 验收标准 `acceptance-criteria.md` | 待更新 |
| 反幻觉 `anti-hallucination.md` | ✅ |
| 记忆系统 `memory-system.md` | ✅ |
