# Paper Agent v4.0 — 后端验收标准

> 对应 [后端开发计划](./development-plan.md)
>
> 日期：2026-07-18

---

## v4.0 新增验收项（25 项）

| # | 验收项 | 级别 |
|---|--------|:---:|
| **Agent 生命周期** |||
| 1 | 注册 → Agent 自动创建 + Redis 心跳开始 | P0 |
| 2 | `GET /api/agents/me/status` 轮询返回正确状态 | P0 |
| 3 | `POST /api/agents/me/start` 异步启动 daemon | P0 |
| 4 | `POST /api/agents/me/stop` 停止 + DEL heartbeat key | P0 |
| 5 | Agent 崩溃后 15s 内 heartbeat key 过期 | P0 |
| 6 | daemon 独立 heartbeat Task 不被 `_run_turn` 阻塞 | P0 |
| **消息路由** |||
| 7 | WS 消息入队时检测 heartbeat → 不存在则 error/AGENT_NOT_RUNNING | P0 |
| 8 | Agent busy 时消息入队 → 推送 status{stage:"queued"} | P1 |
| **Celery 执行** |||
| 9 | plan_approve → `celery_app.send_task("react_execute")` → daemon 立即回到 BRPOP | P0 |
| 10 | Celery Worker react_execute 正确执行 ReAct loop（≤8 轮） | P0 |
| 11 | Celery 任务取消 → outbox_publish(status="cancelled") | P1 |
| **Intent Classify** |||
| 12 | Flash model 7 种意图独立打分 > 0.7 → intents[] | P0 |
| 13 | chat intent → flash reply，不进入 plan | P0 |
| 14 | ops intent → ops_plan → Celery execute | P0 |
| **Plan ⇄ Clarify** |||
| 15 | plan_node 返回 needs_clarification → clarify_node 正确运行 | P0 |
| 16 | clarify_node ReAct ≤5 轮 → 返回 collected_info | P0 |
| 17 | Plan ⇄ Clarify 多轮循环正确（≥3 轮） | P1 |
| **文档管理** |||
| 18 | `POST /api/documents` 支持 mode=create/upload | P0 |
| 19 | `PUT /api/documents/{id}` 乐观锁（version 匹配） | P0 |
| 20 | `GET /api/documents/{id}/versions` 正确处理 4 种 trigger | P1 |
| 21 | `POST /api/documents/{id}/revert/{vid}` 回滚+创建新版本 | P1 |
| **知识库隔离** |||
| 22 | Knowledge 查询按 JWT user_id 自动过滤 | P0 |
| 23 | Knowledge 入库时写入 user_id | P0 |
| **偏好/共享** |||
| 24 | `GET/PUT /api/preferences/me` CRUD | P1 |
| 25 | `POST /api/share` → 细粒度共享流程 | P2 |


## 目录

1. [验收方法论](#1-验收方法论)
2. [Phase 1：基础设施重构](#2-phase-1基础设施重构)
3. [Phase 2：Agent 架构重构](#3-phase-2agent-架构重构)
4. [Phase 3：引用标记与验证](#4-phase-3引用标记与验证)
5. [Phase 4：评估体系与收尾](#5-phase-4评估体系与收尾)
6. [全量回归检查清单](#6-全量回归检查清单)

---

## 1. 验收方法论

### 1.1 验收级别

| 级别 | 说明 | 频率 |
|:---:|------|------|
| **P0** | 阻塞上线，必须通过 | 每个 Phase 出口检查 |
| **P1** | 建议修复，影响体验但不阻塞 | 每个 Phase 出口检查 |
| **P2** | 可延后，记录为 known issue | Phase 4 收尾检查 |

### 1.2 验收方式

| 方式 | 工具 | 说明 |
|------|------|------|
| 自动化测试 | `pytest` | 单元测试 + 端到端测试 |
| 脚本验证 | `python scripts/verify_*.py` | 数据迁移校验 / 检索对比 |
| 手动测试 | 前端 + curl | 涉及 UI 交互的验收项 |
| 日志检查 | `grep` / `journalctl` | 异步任务 / deprecation 日志 |
| 代码审查 | IDE diff | 架构一致性检查 |

### 1.3 验收流程

```
开发完成 → 自测通过 → 提交验收
    │
    ▼
执行验收清单（本文件）
    │
    ├── 全部 P0 通过 → 进入下一 Phase
    ├── P0 未通过 → 修复后重新验收
    └── P1 未通过 → 记录 issue → 进入下一 Phase（不阻塞）
```

---

## 2. Phase 1：基础设施重构

### 2.1 数据库迁移

> 详细迁移步骤见 [数据迁移方案](./data-migration-plan.md)

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **1.1.1** | P0 | PostgreSQL 全部 16 张业务表创建成功 | 脚本验证 | `python scripts/verify_db_schema.py --phase1` 返回 `ALL TABLES OK` |
| **1.1.2** | P0 | pgvector 4 张向量表创建成功 | 脚本验证 | 同上，确认 `paper_chunks`, `glossary_embeddings`, `session_summaries`, `topic_embeddings` 存在 |
| **1.1.3** | P0 | pgvector 索引 IVFFlat 创建成功 | SQL 验证 | `SELECT indexname, indexdef FROM pg_indexes WHERE indexname LIKE '%ivf%'` 返回 2+ 条 |
| **1.1.4** | P0 | SQLite users 表 → PostgreSQL users 表，行数一致 | 脚本验证 | `python scripts/migrate_to_postgres.py --verify users` 输出 `MATCH: 5 rows` |
| **1.1.5** | P0 | SQLite papers 表 → PostgreSQL papers 表，行数一致 + 抽样内容一致 | 脚本验证 | `python scripts/migrate_to_postgres.py --verify papers` |
| **1.1.6** | P0 | SQLite sessions / ws_messages → PostgreSQL，行数一致 | 脚本验证 | `python scripts/migrate_to_postgres.py --verify sessions` |
| **1.1.7** | P0 | ChromaDB paper_chunks → pgvector paper_chunks，行数一致 | 脚本验证 | `python scripts/migrate_to_postgres.py --verify vectors` |

### 2.2 pgvector 检索兼容性

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **1.2.1** | P0 | 10 条固定 query，旧 chroma_store 和新 pgvector_store 的 Top-10 结果完全一致（或 < 4 条差异在可接受范围内） | 脚本验证 | `python scripts/verify_retrieval.py --compare 10` 输出 `COMPATIBLE: 10/10` 或 `COMPATIBLE: 8/10 (2 acceptable diffs)` |
| **1.2.2** | P0 | pgvector_store 支持 metadata 过滤（`user_id` / `paper_id`） | 自动化测试 | `pytest tests/test_pgvector_store.py::test_filter_by_user_id` |
| **1.2.3** | P1 | pgvector 检索延迟：50 万 chunk 下 Top-10 检索 < 500ms | 脚本验证 | `python scripts/benchmark_retrieval.py` 输出 p95 < 500ms |

### 2.3 多用户隔离

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **1.3.1** | P0 | 用户 A 入库 10 篇论文 → 用户 B 查询论文列表返回 0（或只有自己的） | 自动化测试 | `pytest tests/test_multi_user.py::test_papers_isolation` |
| **1.3.2** | P0 | 用户 A 的 ChromaDB 迁移到 pgvector → 用户 B 的检索结果不包含用户 A 的数据 | 自动化测试 | `pytest tests/test_multi_user.py::test_vector_isolation` |
| **1.3.3** | P0 | 用户 A 的会话历史对用户 B 不可见 | 自动化测试 | `pytest tests/test_multi_user.py::test_session_isolation` |
| **1.3.4** | P0 | 无效 Token → 401 响应 | 自动化测试 | `pytest tests/test_multi_user.py::test_invalid_token` |

### 2.4 冷启动引导

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **1.4.1** | P0 | 新用户（papers 表 0 行）发送"你好" → 意图自动切换为 `business`，scenario 为 `literature_survey` | 自动化测试 | `pytest tests/test_cold_start.py::test_empty_kb_triggers_literature` |
| **1.4.2** | P0 | 已有论文的用户发送"你好" → 正常 Chat，不触发引导 | 自动化测试 | `pytest tests/test_cold_start.py::test_non_empty_kb_no_trigger` |
| **1.4.3** | P0 | 引导消息包含"文献调研"关键词 + 可操作按钮（搜索/手动导入） | 手动测试 | 模拟新用户 HTTP 请求，检查返回消息类型为 `question` |

### 2.5 向下兼容

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **1.5.1** | P0 | iOS 客户端发送 HTTP 请求（旧接口路径），响应 200 | 手动测试 | 使用现有 iOS 客户端发送消息 |
| **1.5.2** | P0 | iOS 客户端 WebSocket 消息格式不变，接收正常 | 手动测试 | 使用现有 iOS 客户端接收消息 |
| **1.5.3** | P0 | 无 `user_id` header 的旧请求 → 使用默认 user_id（向后兼容） | 自动化测试 | `pytest tests/test_backward_compat.py::test_no_user_id_header` |

---

## 3. Phase 2：Agent 架构重构

### 3.1 Literature Agent

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **2.1.1** | P0 | 输入 `query="自动驾驶对抗攻击"` → search 节点返回 20+ 篇 arXiv 论文 | 自动化测试 | `pytest tests/test_literature_agent.py::test_search` |
| **2.1.2** | P0 | search → evaluate → 筛选后返回 10-15 篇 | 自动化测试 | `pytest tests/test_literature_agent.py::test_evaluate` |
| **2.1.3** | P0 | download 节点下载 PDF 成功，文件存在且 > 0 字节 | 自动化测试 | `pytest tests/test_literature_agent.py::test_download` |
| **2.1.4** | P0 | convert 节点 PDF → MD，MD 文件存在且包含标题 | 自动化测试 | `pytest tests/test_literature_agent.py::test_convert` |
| **2.1.5** | P1 | LaTeX 密集型论文（公式 > 20 个）自动触发 nougat 转换（如安装） | 手动测试 | 选取一篇 math-heavy 论文 |
| **2.1.6** | P0 | 搜索支持术语词表 Query Expansion（`glossary.search(query, "zh→en")`） | 自动化测试 | `pytest tests/test_literature_agent.py::test_query_expansion` |

### 3.2 Knowledge Agent

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **2.2.1** | P0 | 从 MD 文件 chunk（模式 A），产出 ≥5 个有效 chunk | 自动化测试 | `pytest tests/test_knowledge_agent.py::test_chunk_mode_a` |
| **2.2.2** | P0 | embed 节点：chunk → embedding → pgvector 入库成功 | 自动化测试 | `pytest tests/test_knowledge_agent.py::test_embed` |
| **2.2.3** | P0 | 模式 B（词粒度切片）被 Glossary Sub-Agent 触发成功 | 自动化测试 | `pytest tests/test_knowledge_agent.py::test_chunk_mode_b_triggered_by_glossary` |
| **2.2.4** | P0 | 同标题 + 同期刊论文去重成功（Level 1 精确去重） | 自动化测试 | `pytest tests/test_knowledge_agent.py::test_dedup_exact` |
| **2.2.5** | P0 | CVPR 版覆盖 arxiv 版（权威来源优先） | 自动化测试 | `pytest tests/test_knowledge_agent.py::test_dedup_authority` |
| **2.2.6** | P0 | rag_query：输入 query → 返回 Top-5 chunk + 相关性分数 | 自动化测试 | `pytest tests/test_knowledge_agent.py::test_rag_query` |

### 3.3 Writing Agent

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **2.3.1** | P0 | "帮我写一篇自动驾驶对抗攻击的综述" → 返回结构化综述（含章节标题） | 手动测试 | HTTP 请求 + 检查响应 JSON |
| **2.3.2** | P0 | "用 CVPR 风格写 related work" → 返回的模板包含"子领域分组 + 时间线 + 局限性 + 本文贡献"结构 | 手动测试 | 检查响应 JSON 中 template 字段结构 |
| **2.3.3** | P0 | AI 味黑名单：包含"值得注意的是"的文本被标记为 ⚠️ | 自动化测试 | `pytest tests/test_writing_agent.py::test_ai_flavor_detect` |
| **2.3.4** | P0 | AI 味黑名单：包含"——"的文本被替换为句号 | 自动化测试 | `pytest tests/test_writing_agent.py::test_ai_flavor_replace` |
| **2.3.5** | P0 | "然而"在一段中出现 3 次 → 标记 overuse | 自动化测试 | `pytest tests/test_writing_agent.py::test_ai_flavor_frequency` |

### 3.4 Glossary Sub-Agent

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **2.4.1** | P0 | collect 流水线：10 篇论文 MD → 产出 20+ 术语 | 自动化测试 | `pytest tests/test_glossary_agent.py::test_collect` |
| **2.4.2** | P0 | 产出术语包含 `en_term` / `zh_term` / `df`(≥2) / `cross_doc_score` | 自动化测试 | 检查返回的 GlossaryTerm 列表字段完整性 |
| **2.4.3** | P0 | 学术通用词被过滤（"paper", "method", "result" 不在结果中） | 自动化测试 | `pytest tests/test_glossary_agent.py::test_stop_word_filter` |
| **2.4.4** | P0 | LLM 重排序后 `should_keep=false` 的术语未入库 | 自动化测试 | 检查 glossary_terms 表中不应出现被拒绝的术语 |
| **2.4.5** | P0 | Knowledge Agent 入库完成后 → Celery `glossary_collect` 异步执行 | 自动化测试 | 检查 Celery 任务状态 |
| **2.4.6** | P0 | 术语收集完成后 → Chat 中收到一条普通文本消息："术语词表已更新：提取 X 个专业术语" | 自动化测试 | 监听 outbox 消息队列 |
| **2.4.7** | P0 | 同一术语在第二次文献调研中出现 → `frequency` 自增、`last_seen_at` 更新 | 自动化测试 | `pytest tests/test_glossary_agent.py::test_frequency_accumulation` |
| **2.4.8** | P1 | 术语超过 90 天未出现 → `llm_confidence *= 0.5`；超过 180 天 → 标记为"可能过时"供用户确认 | 自动化测试 | `pytest tests/test_glossary_agent.py::test_confidence_decay` |

### 3.5 架构一致性

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **2.5.1** | P0 | `ingest_graph.py` 调用路径仍有日志输出 `[DEPRECATED]` | 日志检查 | `grep "DEPRECATED" logs/*.log` |
| **2.5.2** | P0 | `history_graph.py` 逻辑相关调用已移除，Store episodes 正常记录 | 代码审查 | `git diff` + 手动验证 |
| **2.5.3** | P0 | `rad_query_graph.py` 导入路径更新为 `knowledge_graph.rag_query` | 代码审查 | `grep -r "from.*rad_query" --include="*.py"` 返回 0 结果 |
| **2.5.4** | P0 | 7 个 Agent 在 `agent_manifest.json` 中均有定义 | 代码审查 | `jq '.agents | keys' agent_manifest.json` |
| **2.5.5** | P0 | tool_registry 中 `glossary_agent.*` 作为共享 tool namespace 注册 | 代码审查 | `grep "glossary_agent" tool_registry.py` |

---

## 4. Phase 3：引用标记与验证

### 4.1 引用标记格式

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **3.1.1** | P0 | RAG 回答："Adversarial attacks pose a significant threat [local:pap-001#sec-3.2] ✓" | 自动化测试 | `pytest tests/test_citation_format.py::test_rag_has_citation` |
| **3.1.2** | P0 | 纯聊天回答："Python 的 GIL 是..." → 无 `[local:xxx]` 或 `[ext:xxx]` 标记 | 自动化测试 | `pytest tests/test_citation_format.py::test_chat_no_citation` |
| **3.1.3** | P0 | Agent 综合判断回答中包含 `[Agent 综合]` 标记 | 自动化测试 | `pytest tests/test_citation_format.py::test_agent_synthesis_marker` |
| **3.1.4** | P0 | 无来源的观点不编造引用（不出现 `[local:fake-id]`） | 自动化测试 | `pytest tests/test_citation_format.py::test_no_fake_citation` |

### 4.2 库内验证

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **3.2.1** | P0 | 引用编号 `[N]` 格式正确且在 References 中存在 → 验证通过 | | 自动化测试 | `pytest tests/test_verifier.py::test_local_valid` |
| **3.2.2** | P0 | paper_id 在 DB 中不存在 → 验证失败，标记 ⚠️[需核查] | | 自动化测试 | `pytest tests/test_verifier.py::test_local_not_found` |
| **3.2.3** | P0 | References 条目完整且编号与正文一致 → 验证通过 | "该方法在 ImageNet 上达到 95%" → chunk 中确实包含此信息 → claim_verified=true | 自动化测试 | `pytest tests/test_verifier.py::test_claim_verified_true` |
| **3.2.4** | P0 | 编号越界或 References 缺条目 → 验证失败，标记异常 | "达到 99%" → chunk 中说 95% → claim_verified=false | 自动化测试 | `pytest tests/test_verifier.py::test_claim_verified_false` |

### 4.3 外部验证

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **3.3.1** | P0 | `[ext:10.1145/3293318.3330783]`（真实 DOI）→ Crossref 验证通过，状态 ✓ | 自动化测试 | `pytest tests/test_external_validator.py::test_crossref_valid` |
| **3.3.2** | P0 | `[ext:10.9999/fake-doi-12345]`（假 DOI）→ 全通道验证失败，状态 ❌ | 自动化测试 | `pytest tests/test_external_validator.py::test_crossref_invalid` |
| **3.3.3** | P0 | arXiv ID 引用 → arXiv API 验证通过 | 自动化测试 | `pytest tests/test_external_validator.py::test_arxiv_valid` |
| **3.3.4** | P0 | Crossref 不可用时 → 自动 fallback 到 Semantic Scholar | 自动化测试 | `pytest tests/test_external_validator.py::test_fallback_to_s2` |
| **3.3.5** | P0 | 外部验证结果缓存：同一 DOI 30 天内不重复查询 | 自动化测试 | `pytest tests/test_external_validator.py::test_cache_hit` |

### 4.4 验证失败处理

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **3.4.1** | P0 | 任一引用 ❌ → 通知用户（普通文本消息） | 自动化测试 | `pytest tests/test_verifier.py::test_notify_on_fail` |
| **3.4.2** | P0 | ❌ 引用 → LLM 自动重生成（最多 2 次），重试后仍失败 → 标记为 ❌ + 建议用户手动核查 | 自动化测试 | `pytest tests/test_verifier.py::test_retry_max_2_times` |
| **3.4.3** | P0 | 重生成后验证通过 → 替换原始回答 → 通知消息不再包含 ❌ | 自动化测试 | `pytest tests/test_verifier.py::test_retry_success` |
| **3.4.4** | P0 | hallucination_events 表记录每次假引用事件 | SQL 验证 | `SELECT COUNT(*) FROM hallucination_events WHERE event_type='fabricated_doi'` |

### 4.5 并行调度

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **3.5.1** | P0 | Literature Agent + Knowledge Agent 无 `depends_on` → 并行执行 | 自动化测试 | `pytest tests/test_parallel.py::test_parallel_lit_knowledge` |
| **3.5.2** | P0 | Literature Agent `depends_on=["0"]` → 等待 0 完成后才启动 | 自动化测试 | `pytest tests/test_parallel.py::test_sequential_first` |
| **3.5.3** | P0 | 并行执行时 WS 推送 `sub_agent/progress` 事件包含 `parallel_group_id` 字段 | WebSocket 监听 | 手动监听 ws，检查 progress 事件 payload |
| **3.5.4** | P0 | Literature Agent → Knowledge Agent 短路径协作：Literature 结果直接传递，不绕 Main Agent | 日志检查 | `grep "short_path" logs/agent.log` |

---

## 5. Phase 4：评估体系与收尾

### 5.1 检索质量评估

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **4.1.1** | P0 | Recall@10 ≥ 0.75 | 脚本验证 | `python scripts/run_evaluation.py` 输出 Recall@10 值 |
| **4.1.2** | P0 | MRR ≥ 0.60 | 脚本验证 | 同上 |
| **4.1.3** | P0 | NDCG@10 ≥ 0.65 | 脚本验证 | 同上 |
| **4.1.4** | P0 | 跨语言检索 Recall@10 对比中文直搜提升 ≥ 0.10 | 脚本验证 | `python scripts/run_evaluation.py --compare-cross-lingual` |
| **4.1.5** | P0 | 测试集包含 3-5 个领域，每领域 10 条 query + 标注 | 代码审查 | `ls test_sets/retrieval/` 返回 3-5 个 JSON 文件 |
| **4.1.6** | P1 | Recall@20 ≥ 0.85 | 脚本验证 | 评估报告显示 |

### 5.2 反幻觉自动化测试

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **4.2.1** | P0 | 假引用测试：10 条假 DOI 引用 → 至少 9 条标记为 ❌ | 自动化测试 | `pytest tests/anti_hallucination.py::test_fake_doi` 通过率 ≥ 90% |
| **4.2.2** | P0 | 声明确认测试：5 条不匹配声明 → 至少 4 条 claim_verified=false | 自动化测试 | `pytest tests/anti_hallucination.py::test_claim_mismatch` 通过率 ≥ 80% |
| **4.2.3** | P0 | 红队测试集包含 20 条测试用例 | 代码审查 | `wc -l test_sets/anti_hallucination/queries.json` 显示 20 条 |
| **4.2.4** | P0 | 自动化脚本输出 PASS/FAIL 汇总 | 自动化测试 | `pytest tests/anti_hallucination.py -v` 输出清晰汇总 |

### 5.3 周度评估任务

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **4.3.1** | P0 | Celery Beat 配置正确，周度评估任务 `evaluate_weekly` 注册 | 代码审查 | `grep "evaluate_weekly" celery_tasks.py` |
| **4.3.2** | P0 | 手动触发 → 评估报告生成到 `reports/YYYY-WXX.md` | 手动测试 | `celery call evaluate_weekly` → 检查文件存在 |
| **4.3.3** | P0 | 评估报告包含：Recall@10 / MRR / NDCG@10 / 引用准确率 / 拒答率 | 手动测试 | `cat reports/2026-W28.md` 检查内容 |

### 5.4 集成工具

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **4.4.1** | P1 | Zotero import：指定 collection → 论文导入到知识库 | 手动测试 | 调用 tool → 检查 papers 表新增 |
| **4.4.2** | P1 | Zotero export：论文列表 → BibTeX 文件格式正确 | 手动测试 | 调用 tool → `bibtex-parser` 验证 |
| **4.4.3** | P2 | Overleaf push：将综述内容写入 Overleaf Git repo | 手动测试 | 调用 tool → 检查 Overleaf 项目同步 |

### 5.5 性能与回归

| AC# | 级别 | 验收项 | 验证方式 | 具体步骤 |
|-----|:---:|--------|----------|----------|
| **4.5.1** | P0 | 10 并发用户同时入库，p95 响应时间 < 30s | 压力测试 | `python scripts/stress_test.py --concurrent 10 --scenario ingest` |
| **4.5.2** | P1 | 10 并发用户同时 RAG 查询，p95 响应时间 < 5s | 压力测试 | `python scripts/stress_test.py --concurrent 10 --scenario rag` |
| **4.5.3** | P0 | iOS 客户端全量回归测试通过 | 手动测试 | 逐功能验证（见 §6） |

---

## 6. 全量回归检查清单

> 以下为 4 个 Phase 全部完成后的全量回归检查，确保重构未破坏现有功能。

### 6.1 核心工作流

| # | 功能 | 操作 | 期望 |
|---|------|------|------|
| R1 | 文献调研 | 发送"调研自动驾驶对抗攻击" | 搜索 → 评估 → 入库 → 提示完成 |
| R2 | 文献调研（冷启动） | 新用户发送任意消息 | 自动引导文献调研 |
| R3 | RAG 问答 | 发送"我知识库中对抗攻击论文的结论是什么" | 返回回答 + `[local:pap-xxx] ✓` |
| R4 | 综述生成 | 发送"帮我写综述" | 返回结构化综述 |
| R5 | 方向聚类 | 发送"我的研究方向有哪些" | 返回聚类结果 |

### 6.2 多用户

| # | 功能 | 操作 | 期望 |
|---|------|------|------|
| R6 | 用户 A 入库 | 用户 A 入库 10 篇 | 用户 A 可见 |
| R7 | 用户 B 隔离 | 用户 B 查询论文 | 看不到用户 A 的论文 |
| R8 | 用户 B 入库 | 用户 B 入库 5 篇 | 用户 B 可见自己的 5 篇 |

### 6.3 iOS 兼容

| # | 功能 | 操作 | 期望 |
|---|------|------|------|
| R9 | 发送消息 | iOS 客户端发送文本 | 正常回复 |
| R10 | 接收消息 | iOS 客户端接收 | WebSocket 格式不变 |
| R11 | 入库进度 | iOS 客户端订阅进度 | 进度事件格式不变 |

---

> **验收结论模板**：
>
> Phase X 验收结论：
> - P0 通过：M/N ✓
> - P0 失败：0
> - P1 待修复：K 项（issue #xxx-#yyy）
> - 验收人：\_______
> - 日期：2026-XX-XX
