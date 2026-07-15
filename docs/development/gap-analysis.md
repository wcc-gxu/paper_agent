# Paper Agent v4 — 查缺补漏与当前客观状态

> 最后更新: 2026-07-15
> 对照 `agent-architecture-v4.md` 目标架构，逐项列出当前代码状态与目标之间的差距。

---

## 1. Agent 架构

### 1.1 当前状态

| Agent | Graph 文件 | 代码行数 | agent_ 工具 | 状态 |
|------|------|:---:|:---:|:---:|
| Supervision | `main_graph.py` | 1509 | — (编排层) | ✅ v3.1 功能完整 |
| Literature | `literature_graph.py` | 329 | 1 | ✅ 功能完整 |
| Knowledge | `knowledge_graph.py` | 1360 | 3 | ✅ 功能完整 |
| Research | `clustering_graph.py` + `citation_chase_graph.py` | 393 + 376 | 1 (旧) | ⚠️ 缺 agent_clustering |
| Writing | `writing_graph.py` | 582 | 2 | ⚠️ 缺 landscape + gap_analysis |
| Translation | `translation_graph.py` | 370 | 2 (旧, v1) | ⚠️ 缺统一 agent_translate |
| Glossary | `glossary_graph.py` | 537 | 1 | ✅ 功能完整 |
| Capture | `video_graph.py` | 708 | 1 | ✅ 功能完整 |

### 1.2 废弃但残留的文件

| 文件 | 行数 | 外部引用 | 清理风险 |
|------|:---:|------|:---:|
| `ingest_graph.py` | 443 | `routes.py:748` | 中 — 需改 API 路由 |
| `rad_query_graph.py` | 234 | `celery_tasks.py:752` | 中 — 需改 Celery 路由 |
| `history_graph.py` | 326 | 无 | **低** — 可直接删除 |

### 1.3 旧工具待清理

| 类别 | 数量 | 文件 |
|------|:---:|------|
| v1 sub_agent 工具 (category="search") | 19 | `tool_registry.py:_register_sub_agent_tools` |
| 其中已有 v2 替代的 | 2 | agent_generate_survey, agent_build_glossary |
| 待迁移到新 Agent 的 | 3 | agent_citation_chase, agent_translate_query, agent_find_related |
| 需降级为非 agent_ 工具的 | 2 | agent_paper_export, agent_paper_clean |
| 功能可删的 | 12 | 见架构文档 §7.1 |

---

## 2. Supervision Agent 节点

### 2.1 当前节点清单 vs v4 目标

| 节点 | 当前存在? | v4 目标 | 差距 |
|------|:---:|------|------|
| fast_triage | ✅ | Judge (不变) | — |
| intent_classify | ✅ | Judge (不变) | — |
| plan | ✅ | Plan (不变) | — |
| plan_review | ✅ | → 合并到 Gate | 需合并 ask_user |
| ops_confirm | ✅ | Judge (不变) | — |
| execute | ✅ | Actor (不变) | — |
| todo_checkpoint | ✅ | Judge (不变) | — |
| evaluate | ✅ | Judge (不变) | — |
| ask_user | ✅ | → 合并到 Gate | 需与 plan_review 合并 |
| inline_reply | ✅ | Responder (不变) | — |
| **clarify** | ❌ | **Actor (NEW)** | **需新建** |
| **Gate** | ❌ | **统一 Gate 模式** | **需合并+统一** |

### 2.2 当前图边 vs v4 目标

```
当前:  plan → (clarify) → ask_user → evaluate → replan → plan
       plan → (plan_review) → plan_review → approve → execute / revise → plan

v4:    plan → (clarify_needed) → clarify (Actor) → plan
       plan → (normal) → Gate.plan_review → approve → execute / revise → clarify (Actor)
       evaluate → ask_user → Gate.ask_user → clarify (Actor)
       evaluate → replan → plan (保留 executed todos)
```

---

## 3. 反幻觉体系

| 层面 | 落地率 | 最大缺口 |
|------|:---:|------|
| L1 Schema + 安全过滤 | 100% | — |
| L2 引用验证 | 86% | RAG 回答未集成 CitationVerifier |
| L3 外部验证 | 83% | 未集成到图中 |
| L4 输出验证 | 11% | output_verify 节点完全缺失 |
| 提示词反虚构 | 25% | 8 个 prompt 未添加 |
| 审计基础设施 | 20% | 表存在但无写入逻辑 |
| **总计** | **55%** | **核心问题是集成，不是模块开发** |

详见 `anti-hallucination.md` 附录 B。

---

## 4. 信息管理体系

### 4.1 碎片知识

| 组件 | 状态 | 问题 |
|------|:---:|------|
| MemoryManager (v1) | 仍在使用 | `tool_registry.py` 7 个 memory 工具仍调 v1 API |
| LangGraph Checkpointer | ✅ 已迁移 | `AsyncPostgresSaver` |
| LangGraph Store | ⚠️ 部分 | 8 namespace 定义完整但 `update_preference` 只写了 preferences |
| SummarizationNode | ✅ 已落地 | 3 档压缩完整 |
| conversation_archive | ✅ 已落地 | dedup + schema fix done |

### 4.2 结构化论文

| 组件 | 状态 | 问题 |
|------|:---:|------|
| papers (22列) | ✅ 完整 | — |
| paper_chunks (向量) | ✅ 完整 | — |
| paper_figures | ✅ 新增 | Phase 4 刚落地 |
| paper_archives | ✅ 新增 | Phase 4 刚落地 |
| journal_ranks | ✅ 完整 | — |
| citations | ✅ 完整 | — |

### 4.3 研究进展追踪

| 组件 | 状态 | 问题 |
|------|:---:|------|
| 关键词订阅 + Beat | ✅ 已有 | subscription_check_task |
| 语义关联发现 | ❌ 缺失 | 需 Literature Agent 定时推送 |
| 话题追踪 | ⚠️ 部分 | topic_embeddings 表存在但使用不充分 |
| APNs 推送 | 🔶 骨架 | aioapns 未真实集成 |

---

## 5. 时区处理

### 5.1 识别到的 datetime 调用点

| 文件 | 调用次数 | 当前 | 目标 |
|------|:---:|------|------|
| `pgdb.py:_now()` | 1 (被广泛调用) | UTC | DB 层保持 UTC |
| `outbox.py` | 2 | UTC | **改北京时间** |
| `task_logger.py` | 1 | UTC | **改北京时间** |
| `agent_error.py` | 1 | UTC | **改北京时间** |
| `reporter.py` | 1 | UTC | **改北京时间** |
| `daemon.py` | 1 | UTC | **改北京时间** |
| `celery_tasks.py` | 1 | UTC | 保持 UTC (Celery 内部) |
| `llm_client_v2.py` | 2 (当前日期提示) | `datetime.now()` (UTC) | **改北京时间** |
| `tool_registry.py` | 2 | UTC | **改北京时间** |
| `memory.py` | 4 | UTC | **改北京时间** |
| `summarizer.py` | 1 | UTC | **改北京时间** |

### 5.2 修改策略

1. 新增 `src/paper_search/utils/datetime_utils.py` — `beijing_now()`, `beijing_now_iso()`, `utc_to_beijing()`
2. 所有用户面 timestamp (outbox/error/reporter) → `beijing_now_iso()`
3. `pgdb.py:_now()` 保持 UTC（DB 存储层不变）
4. Pydantic 模型 `created_at`/`updated_at` 序列化时自动转北京时间

---

## 6. Plan 升级——Todo 验收标准

### 6.1 当前 PlanOutput/TodoSpec

```python
class TodoSpec(BaseModel):
    id: str
    label: str                    # 简短描述 (120 chars)
    tool_calls: list[ToolCallSpec]
    parallel: bool
    success_criterion: str        # 验收标准 (200 chars) ← 太短
```

### 6.2 待升级项

| 项目 | 当前 | 目标 |
|------|------|------|
| success_criterion 长度 | 200 chars | 500 chars — 支持量化指标 |
| 缺少 depends_on 链 | `ToolCallSpec.depends_on` 存在但未在 Plan 层暴露 | PlanOutput 需包含 todo 间的依赖声明 |
| 缺少 verify_method | 只有 success_criterion | 新增 verify_method: "tool_output" / "llm_check" / "user_confirm" |
| 缺少 expected_output | 无 | 新增 expected_output_keys: list[str] — 下游 todo 消费哪些字段 |
| PlanOutput reasoning 字段 | 200 chars | 500 chars — 审计需要更完整的推理链 |

---

## 7. Plan 与 Writing Agent 协同

### 7.1 当前

- Plan 节点独立运行，不感知 Writing Agent 的内部能力
- `agent_generate_survey_v2` 被当作黑盒工具调用

### 7.2 v4 目标

- Plan 节点知道 Writing Agent 包含 landscape + gap_analysis
- 用户可以要求 "生成综述 + 分析研究空白"
- Plan 生成 todos:
  ```
  1. agent_literature_search → 搜索论文
  2. agent_knowledge_ingest → 入库
  3. agent_generate_survey_v2(template=arxiv, include_gap_analysis=true) → 生成综述含空白分析
  ```

---

## 8. 文档完整度评估

| 文档 | 存在? | 完整度 | 待更新 |
|------|:---:|:---:|------|
| Agent 架构设计 | ✅ `agent-architecture-v4.md` | 🔶 初稿 | 待讨论确认 |
| 反幻觉策略 | ✅ `anti-hallucination.md` | ✅ 正文完整 | 附录 B 已追加 |
| 反幻觉实现进度 | ✅ (同上附录 B) | ✅ | — |
| 查缺补漏 | ✅ `gap-analysis.md` (本文档) | 🔶 初稿 | 持续更新 |
| 记忆系统 | ✅ `memory-system.md` | ✅ | v2 迁移状态更新 |
| 数据库架构 | ✅ `database-architecture.md` | ✅ | Phase 4 新增表已同步 |
| WebSocket 协议 | ✅ `websocket-protocol.md` | ✅ | Gate 合并后需更新 |
| API 参考 | ✅ `api-reference.md` | ✅ | agent_ 工具变更后更新 |
| 产品规格 | ✅ `product/product-spec.md` | ✅ | — |
| 产品架构计划 | ✅ `product/product-architecture-plan.md` | ✅ | 对照 v4 更新 |

---

## 9. 实施优先级矩阵

### P0 — 阻塞性 (必须做)

| # | 任务 | 工作量 | 文件 |
|---|------|:---:|------|
| 1 | 新建 `clarify` 节点 (Actor+Restricted tools) | 2d | `main_graph.py` |
| 2 | 合并 plan_review + ask_user → Gate | 1d | `main_graph.py` + `daemon.py` |
| 3 | plan 新增 clarify_needed 字段 | 0.5d | `main_agent_prompts.py` |
| 4 | 删除 3 个废弃 graph 文件 + 清除引用 | 2h | `ingest_graph.py`, `rad_query_graph.py`, `history_graph.py` |
| 5 | 删除 19 个 v1 agent_ 工具 | 2h | `tool_registry.py` |

### P1 — 重要 (应该做)

| # | 任务 | 工作量 | 文件 |
|---|------|:---:|------|
| 6 | 新增 `agent_clustering` | 2h | `tool_registry.py` |
| 7 | 新增 `agent_translate` (统一 Translation 入口) | 1h | `tool_registry.py` |
| 8 | Knowledge Agent RAG 集成 CitationVerifier | 3h | `knowledge_graph.py` |
| 9 | L4 output_verify 节点 (依赖 L2+L3 集成) | 2d | `main_graph.py` |
| 10 | Writing Agent landscape + gap_analysis 节点 | 2d | `writing_graph.py` |
| 11 | 时区工具函数 + Pydantic validator | 2h | `utils/datetime_utils.py` + models |

### P2 — 增强 (可以做)

| # | 任务 | 工作量 | 文件 |
|---|------|:---:|------|
| 12 | 8 个 prompt 添加 ANTI_FABRICATION_CLAUSE | 1h | `main_agent_prompts.py` |
| 13 | hallucination_events 写入逻辑 | 2h | `main_graph.py` + `pgdb.py` |
| 14 | success_criterion 长度 200→500 + 新增字段 | 30min | `main_agent_prompts.py` |
| 15 | bash_query 工具 (只读 shell) | 2h | `tool_registry.py` |
| 16 | 文档更新 (CLAUDE.md, memory-system.md, api-reference.md) | 2h | 各文档 |
| 17 | Literature Agent 语义关联推送 (Celery Beat) | 1d | `celery_tasks.py` |

### P3 — 远期

| # | 任务 | 工作量 | 说明 |
|---|------|:---:|------|
| 18 | ExternalValidator 集成到 output_verify | 1d | 依赖 P1-#9 |
| 19 | groundedness LLM judge | 1d | 依赖 P1-#9 |
| 20 | revise loop | 2d | 依赖 P1-#9 |
| 21 | APNs 真实集成 (aioapns) | 3d | 已有骨架 |
