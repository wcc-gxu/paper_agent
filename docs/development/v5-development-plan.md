# Paper Agent v5 — 分阶段开发计划

> 基于 [架构升级方案 v5](architecture-upgrade-v5.md) | 2026-07-20

---

## 总览

| 阶段 | 预估 | 风险 | 产出 | 状态 |
|------|:---:|:---:|------|:---:|
| Phase 0: 死代码清理 | 2-3h | 低 | 安全删除 dead code | ✅ |
| Phase 1: 主图简化 | 4-6h | 中 | 新 Graph 结构跑通 | ✅ |
| Phase 2a: RAG Handler | 3-4h | 中 | **RAG 可演示** | ✅ |
| Phase 2a+: ingest/cleanup/survey | 3h | 中 | 入库+清理+调研 handler | ✅ |
| Phase 2b: 其余 Handler | 6-8h | 中 | 全部 handler 可用 | 🔧 |
| Phase 3: Celery 集成 | 3-4h | 中 | 异步下载/入库 | 🔧 |
| Phase 4: 跨 Turn 状态 | 3-4h | 中 | 多 turn 上下文 | 🔧 |
| Phase 5: 文档更新 | 4-5h | 低 | 文档对齐 | 🔧 |
| **合计** | **28-37h** | — | — | — |

---

## Phase 0: 死代码清理（安全，不动逻辑）

### 0.1 删除文件

| 操作 | 文件 | 理由 |
|------|------|------|
| 删除 | `src/paper_search/agent/react_executor.py` | 从未被 dispatch，功能破损 |

### 0.2 删除函数/类/字典

| 操作 | 对象 | 文件 |
|------|------|------|
| 删除 | `SCENARIOS` 字典 | `main_agent_prompts.py` |
| 删除 | `SCENARIO_IDS` 列表 | `main_agent_prompts.py` |
| 删除 | `SubStepSpec` Pydantic model | `main_agent_prompts.py` |
| 删除 | `PlanReviewPayload` | `main_agent_prompts.py` |
| 删除 | `PlanTodoUpdatePayload` | `main_agent_prompts.py` |
| 删除 | `ToolExecutionPayload` | `main_agent_prompts.py` |
| 删除 | `build_plan_review_prompt()` 函数 | `main_agent_prompts.py` |
| 删除 | `run_pipeline_via_celery()` 方法 | `sub_agent.py` |
| 删除 | `sub_agent_task` 及其下游 7 个 stage task | `celery_tasks.py` |
| 删除 | `_run_graph_agent()` / `_run_graph_agent_async()` | `celery_tasks.py` |
| 删除 | `"paper_search.agent.react_executor"` autodiscover | `celery_app.py` |

### 0.3 修复 Bug

| 操作 | 文件 | 说明 |
|------|------|------|
| 修复 | `routes.py:807` | `IngestAgent` 不存在 → 改为调 `LiteratureAgent` + `KnowledgeAgent` |

### 0.4 清理 Import

| 操作 | 文件 | 删除内容 |
|------|------|------|
| 删除 import | `main_graph.py:37-40` | `build_plan_review_prompt`, `PlanReviewPayload`, `PlanTodoUpdatePayload`, `ToolExecutionPayload` |

### 0.5 清理 `__all__`

| 操作 | 文件 |
|------|------|
| 删除 export | `main_agent_prompts.py` 中 `SCENARIOS`, `SCENARIO_IDS`, `SubStepSpec`, `PlanReviewPayload`, `PlanTodoUpdatePayload`, `ToolExecutionPayload`, `build_plan_review_prompt` |

### 验证

```bash
PYTHONPATH=src pytest tests/ -v
```

---

## Phase 1: 主图简化

### 1.1 修改 `main_agent_prompts.py`

**简化 Intent Classify Prompt**

```python
INTENT_CLASSIFY_V5_PROMPT = """你是意图分类器。对用户输入分类：

Primary Intent (选一个):
- rag: 知识库问答
- survey: 文献调研/搜索
- translation: 学术翻译
- writing: AI 辅助写作
- glossary: 词表管理
- paper_analysis: 论文精读
- clustering: 研究方向聚类
- citation_chase: 引用追溯
- knowledge_mgmt: 知识管理（入库/订阅）
- chat: 闲聊/通用问答
- ops: 运维操作

Side Intents (0-N 个，辅助意图):
- preference: 用户偏好声明
- feedback: 对之前回答的反馈
- mentor_quote: 导师语录

输出 JSON:
{
  "primary": "rag",
  "side": [{"type": "preference", "content": "只看CCF-A期刊"}],
  "params": {"question": "attention机制有哪些改进？", ...},
  "route": "rag"
}"""

# 删除的 prompt 函数:
# - INTENT_CLASSIFY_PROMPT (替换为 v5 版本)
# - PLAN_V31_SYSTEM (不再需要 plan)
# - EVALUATE_V31_SYSTEM (不再需要 evaluate)
# - TODO_CHECKPOINT_SYSTEM (不再需要 checkpoint)
# - all build_*_v31_prompt() 函数 (不再使用)
```

### 1.2 简化 `MainState`

```python
class MainState(TypedDict, total=False):
    # Input
    user_content: str
    session_id: str
    correlation_id: str

    # Fast Triage
    triage_chat: float
    triage_ops: float
    triage_research: float
    triage_reasoning: str

    # Intent
    primary_intent: str
    side_intents: list[dict]
    intent_params: dict
    route: str

    # Side Handler
    side_processed: bool

    # Ops
    ops_confirmed: bool
    danger_level: str

    # Handler 通用
    final_reply: str
    error: Optional[str]
    _reply_pushed: bool
```

### 1.3 新 `compile()` 结构

```python
def compile(self, checkpointer=None):
    builder = StateGraph(MainState)

    builder.add_node("fast_triage", self._fast_triage)
    builder.add_node("intent_classify", self._intent_classify)
    builder.add_node("side_handler", self._side_handler)
    builder.add_node("ops_confirm", self._ops_confirm)
    builder.add_node("execute", self._execute)  # ops 路径复用的 ReAct
    builder.add_node("inline_reply", self._inline_reply)
    # Handler 节点
    builder.add_node("rag_handler", self._rag_handler)
    builder.add_node("literature_search_handler", self._literature_search_handler)
    builder.add_node("download_handler", self._download_handler)
    builder.add_node("convert_handler", self._convert_handler)
    builder.add_node("ingest_handler", self._ingest_handler)
    builder.add_node("survey_handler", self._survey_handler)
    builder.add_node("translate_handler", self._translate_handler)
    builder.add_node("writing_handler", self._writing_handler)
    builder.add_node("glossary_handler", self._glossary_handler)
    builder.add_node("cluster_handler", self._cluster_handler)
    builder.add_node("citation_handler", self._citation_handler)
    builder.add_node("paper_handler", self._paper_handler)

    # Edges
    builder.add_edge(START, "fast_triage")

    builder.add_conditional_edges("fast_triage", self._route_triage, {
        "research": "intent_classify",
        "ops": "ops_confirm",
        "chat": "inline_reply",
    })

    builder.add_conditional_edges("intent_classify", self._route_intent, {
        "side_handler": "side_handler",
        "chat": "inline_reply",
        "ops": "ops_confirm",
    })

    builder.add_conditional_edges("side_handler", self._route_to_handler, {
        "rag": "rag_handler",
        "survey": "literature_search_handler",
        "download": "download_handler",
        "convert": "convert_handler",
        "ingest": "ingest_handler",
        "survey_generate": "survey_handler",
        "translation": "translate_handler",
        "writing": "writing_handler",
        "glossary": "glossary_handler",
        "clustering": "cluster_handler",
        "citation_chase": "citation_handler",
        "paper_analysis": "paper_handler",
        "knowledge_mgmt": "ingest_handler",
    })

    builder.add_conditional_edges("intent_classify", self._route_ops_or_chat, {
        "ops": "ops_confirm",
        "chat": "inline_reply",
    })

    builder.add_edge("ops_confirm", "execute")

    # 所有 handler → END
    for node in ["rag_handler", "literature_search_handler", "download_handler",
                 "convert_handler", "ingest_handler", "survey_handler",
                 "translate_handler", "writing_handler", "glossary_handler",
                 "cluster_handler", "citation_handler", "paper_handler",
                 "execute", "inline_reply"]:
        builder.add_edge(node, END)

    return builder.compile(checkpointer=checkpointer)
```

### 1.4 新增路由方法

- `_route_to_handler()`: 从 `state["route"]` 读取目标 handler 名称
- `_route_ops_or_chat()`: intent_classify 后 chat/ops 分流

### 1.5 删除方法

| 删除 | 方法 |
|------|------|
| 节点 | `_plan()`, `_clarify()`, `_gate()`, `_todo_checkpoint()`, `_evaluate()` |
| 路由 | `_route_plan()`, `_route_gate()`, `_route_after_execute()`, `_route_todo_checkpoint()`, `_route_evaluate()` |

### 1.6 去掉 WS 信封 `role` 字段

`main_agent.py` `_push()` 中删除 `"role": role` 和 `"priorityKind": priority_kind` 两行。

### 验证

```bash
PYTHONPATH=src pytest tests/ -v
# 手动 WebSocket 测试: 发 chat 消息，收 inline_reply
```

---

## Phase 2a: RAG Handler（面试优先交付）

### 2.1 实现 `_rag_handler()`

```python
async def _rag_handler(self, state: MainState) -> dict:
    session_id = state.get("session_id", "main")
    params = state.get("intent_params", {})
    question = params.get("question") or state.get("user_content", "")

    await self._push_status(session_id, "searching", "正在搜索知识库...")

    try:
        # Call existing agent_knowledge_ask tool
        tool = self.registry.get("agent_knowledge_ask")
        if not tool:
            raise RuntimeError("agent_knowledge_ask tool not found")

        result_json = await tool.ainvoke({"question": question})
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
        answer = result.get("answer") or result.get("result") or str(result)

        await self._push_status(session_id, "done", "完成")
        await self._push(session_id, "message", "reply", "assistant",
                         payload={"content": answer})

        # Push feedback ask
        await self._push(session_id, "ask", "", "assistant",
                         payload={
                             "ask_id": f"feedback-{uuid.uuid4().hex[:8]}",
                             "kind": "confirm",
                             "prompt": "答案有帮助吗？需要重新搜索吗？",
                         })

        return {"final_reply": answer, "_reply_pushed": True}

    except Exception as e:
        # 错误处理: retry → ask
        return await self._handle_handler_error(session_id, e, "RAG 问答")
```

### 2.2 实现 `_handle_handler_error()`

```python
async def _handle_handler_error(self, session_id, error, context):
    """统一错误处理: retry_once → degrade → ask_user."""
    logger.warning(f"{context} 失败: {error}")
    await self._push_error(session_id, f"{context} 失败: {error}",
                           subtype="TASK_FAILED")
    return {"error": str(error), "_reply_pushed": True}
```

### 2.3 Handler 通用模版（供后续 handler 复用）

```python
async def _run_handler(self, state, handler_name, work_fn):
    """Handler 通用运行器: status → work → reply."""
    session_id = state.get("session_id", "main")
    await self._push_status(session_id, "executing", f"正在{handler_name}...")
    try:
        result = await work_fn(state)
        await self._push_status(session_id, "done", "完成")
        await self._push(session_id, "message", "reply", "assistant",
                         payload={"content": str(result)})
        return {"final_reply": str(result), "_reply_pushed": True}
    except Exception as e:
        return await self._handle_handler_error(session_id, e, handler_name)
```

### 验证

```bash
# 启动服务
bash scripts/start-all.sh
# 或 docker compose up -d

# WebSocket 测试 RAG
# 发 {"type":"message","payload":{"content":"库里关于attention的论文有哪些？"}}
# 收 status→searching → message/reply(答案) → ask(反馈确认)
```

---

## Phase 2b: 其余 Handler（面试后实现）

### Handler 清单

| Handler | 工具序列 | 特殊逻辑 |
|---------|---------|---------|
| `_translate_handler` | glossary_search → LLM translate | glossary_search 是 tool，翻译 LLM 在 handler 内调 |
| `_literature_search_handler` | search_papers → evaluate_papers | query 模糊→ask 先澄清；返回结果列表→turn 结束 |
| `_download_handler` | download_paper(s) | N<10→inline；N≥10→ask(confirm)→Celery |
| `_convert_handler` | convert_to_md | Celery task，progress 推送 |
| `_ingest_handler` | chunk_embed_ingest | Celery task，progress 推送 |
| `_survey_handler` | LLM generate survey | 从已下载论文生成综述 |
| `_writing_handler` | generate_survey / check_ai_flavor / gap_analysis | 三个独立 LLM 调用串行 |
| `_glossary_handler` | collect_terms → verify_terms | 校验用 LLM |
| `_cluster_handler` | cluster_papers | K-means + LLM label |
| `_citation_handler` | resolve → fetch_citations → filter_relevance → summarize | 条件边控制追多层 |
| `_paper_handler` | search_kb(get_fulltext) → LLM extract | — |

---

## Phase 3: Celery 集成

### 3.1 需要 Celery 的节点

| 节点 | 触发条件 | Celery task |
|------|---------|------------|
| `download_handler` | 论文数 ≥ 10（ask 确认后） | `download_papers_task` |
| `convert_handler` | 总是 | `convert_papers_task` |
| `ingest_handler` | 总是 | `ingest_papers_task` |

### 3.2 Celery → outbox → WS 进度推送

```
Celery task:
  → Redis: LPUSH outbox:{uid} tool/progress
  → [outbox_poller] BRPOP → WS send → 用户看到进度条
  → 完成后 LPUSH tool/result + message/reply
```

### 3.3 进度恢复

客户端重连后，通过 `GET /api/sessions/{id}/messages` 拉取最近的 `tool/*` 消息复原进度条。

---

## Phase 4: 跨 Turn 状态管理

### 4.1 Redis Session State Schema

```json
{
  "session:{sid}": {
    "last_search_result": {
      "papers": [{"id": "...", "title": "...", "abstract": "...", "relevance": 0.95}],
      "total": 50,
      "timestamp": "2026-07-20T..."
    },
    "last_action": "literature_search",
    "pending_downloads": ["paper_id_1", "paper_id_2"],
    "has_md": {"paper_id_1": true, "paper_id_2": false},
    "has_ingested": {"paper_id_1": true}
  }
}
```

### 4.2 `message/reply` Payload 增强

```json
{
  "type": "message",
  "subType": "reply",
  "payload": {
    "content": "## 搜索结果\n\n...",
    "action": "literature_search_results",
    "papers": [{"id": "...", "title": "...", "abstract": "..."}],
    "next_actions": ["download", "survey", "refine_search"]
  }
}
```

客户端根据 `next_actions` 展示快捷操作按钮。

---

## Phase 5: 文档更新

### 5.1 更新清单

| 优先级 | 文件 | 说明 |
|:---:|------|------|
| P0 | `CLAUDE.md` | 主架构概览、图结构、节点列表 |
| P0 | `docs/development/development-plan.md` | 改为 v5 |
| P0 | `docs/development/agent-architecture-v4.md` | 改名 v5，更新内容 |
| P1 | `docs/development/plangraph-routing.md` | intent→handler 路由表 |
| P1 | `docs/development/gap-analysis.md` | 重新评估 gap |
| P1 | `docs/development/acceptance-criteria.md` | 更新验收标准 |
| P2 | `docs/development/websocket-protocol.md` | status stage 值 |
| P2 | `docs/development/memory-system.md` | handler 上下文 |
| P3 | `docs/product/` | 产品方案 |

### 5.2 废弃文档标记

以下文件在文件头添加 `[DEPRECATED — 请参阅 architecture-upgrade-v5.md]`：

- `docs/development/main-agent.md`
- `docs/development/main-agent-v2-design.md`
- `docs/development/backend-development-plan.md`

---

> 相关文档:
> - [架构升级方案 v5](architecture-upgrade-v5.md)
> - [WebSocket 协议 v11.1](websocket-protocol.md)
