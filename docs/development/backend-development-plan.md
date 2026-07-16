# [DEPRECATED] 智驭·研 v3 后端开发计划

> 基于 [v3 重构方案](../product/智驭研_重构方案_v3.md) + [数据库架构设计](./database-architecture.md)
>
> 日期：2026-07-10

---

## 目录

1. [概述](#1-概述)
2. [总体架构目标](#2-总体架构目标)
3. [Phase 1：基础设施重构](#3-phase-1基础设施重构-week-12)
4. [Phase 2：Agent 架构重构](#4-phase-2agent-架构重构-week-34)
5. [Phase 3：引用标记与验证](#5-phase-3引用标记与验证-week-56)
6. [Phase 4：评估体系与收尾](#6-phase-4评估体系与收尾-week-78)
7. [风险与依赖](#7-风险与依赖)

---

## 1. 概述

### 1.1 约束条件

| 约束 | 说明 |
|------|------|
| 代码尽量不动 | `main_agent.py` 6 节点 StateGraph 主体逻辑不动，仅做必要扩展 |
| 向下兼容 | 迁移过程中不影响已有功能（现有 iOS 客户端仍可用） |
| 私有化部署 | 所有组件（PostgreSQL / pgvector / Redis / Celery）均在目标机器部署 |
| 多用户隔离 | 通过 `user_id` 列级过滤实现，无需多套数据库实例 |

### 1.2 后端技术栈

| 组件 | 技术 | 版本要求 |
|------|------|----------|
| 语言 | Python | 3.11+ |
| 数据库 | PostgreSQL + pgvector | 16+ / 0.8+ |
| 缓存/队列 | Redis | 7+ |
| 异步任务 | Celery + Redis broker | 5.4+ |
| Agent 框架 | LangGraph | >=0.6 |
| WebSocket | FastAPI + websockets | 0.115+ |
| LLM | doubao / 火山引擎 API | — |

### 1.3 4 个阶段总览

```
Phase 1: ████████░░  2 周（基础设施：PostgreSQL + 多用户 + 冷启动）
Phase 2: ████████░░  2 周（Agent 重构：拆分 + Glossary + Celery）
Phase 3: ████████░░  2 周（引用验证：标记格式 + 双通道校验 + 并行调度）
Phase 4: ████████░░  2 周（评估体系：检索质量 + 反幻觉 + 收尾集成）
─────────────────────────────────────────────────────────────────
总计：8 周
```

---

## 2. 总体架构目标

### 2.1 重构前 → 重构后

| 维度 | 当前 | 目标 |
|------|------|------|
| 数据库 | SQLite + ChromaDB | PostgreSQL + pgvector |
| Agent 数 | 代码层 5 个 graph（边界模糊） | 代码层 7 个 Agent（边界清晰） |
| ingest_graph | 7 阶段单体 graph | 拆为 Literature Agent + Knowledge Agent |
| 术语词表 | 无 | Glossary Sub-Agent（7 号 Agent） |
| 引用验证 | 仅 DOI 格式校验 | 内外双通道 + 声明级校验 + 标记格式 |
| 用户模型 | 单一用户 | `user_id` 多用户隔离 |
| 冷启动 | 无 | 检测空知识库 → 引导文献调研 |
| 子 Agent 调度 | 串行 | 并行 + 短路径协作 + 共享上下文池 |

### 2.2 7 Agent 清单

| # | Agent | Graph 文件 | 核心能力 |
|---|-------|-----------|----------|
| 1 | **Main Agent** | `main_graph.py` | 路由、意图、编排、并行调度（不动主体，仅扩展） |
| 2 | **Literature Agent** | `literature_graph.py` | 搜索、下载、筛选、PDF→MD |
| 3 | **Knowledge Agent** | `knowledge_graph.py` | 切片、embedding、入库、去重、RAG |
| 4 | **Research Agent** | `clustering_graph.py` + `citation_chase_graph.py` | 聚类、方法对比、趋势分析、空白发现 |
| 5 | **Writing Agent** | `writing_graph.py` | 综述、模板、引用标记、AI 味校验 |
| 6 | **Capture Agent** | `video_graph.py` | 碎片采集（网页/笔记/音视频） |
| 7 | **Glossary Sub-Agent** | `glossary_graph.py` | 术语收集/查询/进化 |

### 2.3 新增后端文件（count）

| 类别 | 数量 | 示例 |
|------|:---:|------|
| 新建 graph 文件 | 4 | `literature_graph.py`, `knowledge_graph.py`, `writing_graph.py`, `glossary_graph.py` |
| 新建工具文件 | 3 | `pgvector_store.py`, `external_validator.py`, `celery_tasks.py` |
| 修改文件 | 8 | `main_agent.py`, `db.py`, `routes.py`, `daemon.py`, `verifier.py`, `tool_registry.py`, `reporter.py`, `store.py` |
| 废弃文件 | 2 | `chroma_store.py`, `history_graph.py` |
| 评估文件 | 2 | `evaluator.py`, `test_sets/` |

---

## 3. Phase 1：基础设施重构（Week 1-2）

### 3.1 目标

- PostgreSQL + pgvector 环境就绪，数据迁移完成
- 多用户 Token 认证可用
- 冷启动引导流程可用
- 现有功能不受影响（灰度切换）

### 3.2 任务列表

| # | 任务 | 文件 | 估时 | 依赖 |
|---|------|------|:---:|------|
| **1.1** | PostgreSQL + pgvector 部署 + 执行 `init_db.sql` 创建全部 schema | 新建 `scripts/init_db.sql` | 1d | 目标机器 PostgreSQL 16+ |
| **1.2** | 数据迁移脚本：SQLite 业务数据 → PostgreSQL（详见 [迁移方案](./data-migration-plan.md)） | 新建 `scripts/migrate_to_postgres.py` | 1d | 1.1 |
| **1.3** | 向量迁移脚本：ChromaDB collections → pgvector tables（详见 [迁移方案](./data-migration-plan.md)） | 同上脚本 | 1d | 1.1 |
| **1.4** | pgvector 封装层实现（替代 chroma_store.py） | 新建 `pgvector_store.py` | 1d | 1.1 |
| **1.5** | `config.py` 切换数据库连接（支持 PostgreSQL + 双写灰度） | `config.py` | 0.5d | 1.4 |
| **1.6** | 多用户 Token 认证（Bearer Token + user_id 解析） | `auth.py` | 0.5d | — |
| **1.7** | `agent_manifest.json` 按 user_id 隔离 | `daemon.py` | 1d | 1.6 |
| **1.8** | 冷启动引导：检测空知识库 → 覆盖 intent → 引导文献调研 | `main_agent.py` `_node_intent_classify` | 1d | 1.6 |
| **1.9** | 所有 API routes 增加 `user_id` 过滤中间件 | `routes.py` | 0.5d | 1.6 |
| **1.10** | DB DAO 层所有查询增加 `user_id` 过滤（事务安全） | `db.py` | 1d | 1.6 |
| **1.11** | 灰度切换 + 回归测试（iOS 客户端兼容性验证） | 全部 | 1d | 1.2-1.10 |

### 3.3 验收标准

> 详细验收清单见 [验收标准文档 §Phase 1](./acceptance-criteria.md#phase-1-基础设施重构)

| # | 验收项 | 验证方式 |
|---|--------|----------|
| AC1.1 | PostgreSQL 全部 16 张业务表 + 4 张向量表创建成功 | `psql -c "\dt"` |
| AC1.2 | SQLite → PostgreSQL 数据迁移后行数一致 | 自动化校验脚本 |
| AC1.3 | ChromaDB → pgvector 迁移后向量数量一致 + 抽样检索结果一致 | 自动化校验脚本 |
| AC1.4 | pgvector_store 的检索接口与旧 chroma_store 结果兼容 | 10 条固定 query 对比 |
| AC1.5 | iOS 客户端使用旧 Token 和新 Token 均正常 | 手动测试 |
| AC1.6 | 两个用户分别入库论文，互不可见 | 自动化测试 |
| AC1.7 | 空知识库用户触发任意消息 → 引导文献调研 | 自动化测试 |
| AC1.8 | 已有知识库用户触发消息 → 正常 Chat（不引导） | 自动化测试 |

---

## 4. Phase 2：Agent 架构重构（Week 3-4）

### 4.1 目标

- ingest_graph 拆分为 Literature Agent + Knowledge Agent
- Writing Agent 具备综述/模板/引用标记能力
- Glossary Sub-Agent 就绪（术语收集 + Celery 异步编排）
- Capture Agent 命名统一

### 4.2 任务列表

| # | 任务 | 文件 | 估时 | 依赖 |
|---|------|------|:---:|------|
| **2.1** | 从 ingest_graph 拆分 Literature Agent | 新建 `graphs/literature_graph.py` | 1.5d | Phase 1 |
| **2.2** | 从 ingest_graph 拆分 Knowledge Agent（含双模式切片 + pgvector 入库） | 新建 `graphs/knowledge_graph.py` | 1.5d | 2.1 |
| **2.3** | Writing Agent 新建：综述生成 + 模板推荐 | 新建 `graphs/writing_graph.py` | 1d | — |
| **2.4** | Writing Agent 扩展：AI 味校验（黑名单 + 正则 + LLM judge） | `writing_graph.py` | 0.5d | 2.3 |
| **2.5** | Glossary Sub-Agent 实现：collect 流水线（TF-IDF 提取 + LLM 翻译重排序 + 入库） | 新建 `graphs/glossary_graph.py` | 1.5d | 2.2 |
| **2.6** | 术语词表 pgvector 表 + CRUD API 实现 | `db.py`, `routes.py` | 0.5d | 2.5 |
| **2.7** | Celery 异步任务：`glossary_collect` + outbox 消息推送 | 新建 `celery_tasks.py` | 0.5d | 2.5 |
| **2.8** | Main Agent `execute_plan` 改造：支持拆分后的 Agent 调用 | `main_agent.py` | 0.5d | 2.1-2.5 |
| **2.9** | Capture Agent 改名 + `video_graph.py` 注册到 tool_registry | `tool_registry.py` | 0.5d | — |
| **2.10** | `history_graph.py` 逻辑并入 Store episodes | `store.py` | 1d | — |
| **2.11** | `rad_query_graph.py` + `knowledge.py` 统一到 Knowledge Agent 的 rag_query 节点 | `knowledge_graph.py` | 0.5d | 2.2 |
| **2.12** | deprecate 标记：`ingest_graph.py`、`history_graph.py` | 注释 + 日志 | 0.5d | 2.1, 2.2, 2.10 |

### 4.3 验收标准

| # | 验收项 | 验证方式 |
|---|--------|----------|
| AC2.1 | Literature Agent 独立执行：search → evaluate → download → convert → extract_metadata | 自动化端到端测试 |
| AC2.2 | Knowledge Agent 独立执行：chunk（双模式） → embed → dedup → rag_query | 自动化端到端测试 |
| AC2.3 | 用户说"帮我写 CVPR 风格的 related work" → Writing Agent 返回结构化的 related work + 模板推荐 | 手动测试 |
| AC2.4 | 用户说"帮我去 AI 味" → 检测到"——"并替换 | 自动化测试 |
| AC2.5 | Literature Agent 入库完成后 → Celery 异步触发术语收集 → 完成后 Chat 中收到一条文本通知 | 自动化端到端测试 |
| AC2.6 | glossary_terms 表有新术语插入，且包含 en_term/zh_term/df/llm_confidence | SQL 查询验证 |
| AC2.7 | 旧版 ingest_graph 调用路径仍可用但输出 deprecation 日志 | 日志检查 |
| AC2.8 | `rad_query_graph` 的调用路径指向 `knowledge_graph.rag_query` | 手动代码审查 |

---

## 5. Phase 3：引用标记与验证（Week 5-6）

### 5.1 目标

- 所有 LLM prompt 输出引用标记格式 `[local:xxx]` / `[ext:doi]` / `[Agent 综合]`
- 库内验证通道（三步校验）+ 外部验证通道（Crossref / arXiv / S2）正式接入
- 并行子 Agent 调度就绪

### 5.2 任务列表

| # | 任务 | 文件 | 估时 | 依赖 |
|---|------|------|:---:|------|
| **3.1** | ScenarioPlanResult 增加 `output_format` / `verification_mode` 字段 | `main_agent_prompts.py` | 0.5d | Phase 2 |
| **3.2** | LLM prompt 改造（8 个 prompt 文件）：强制引用标记格式 | `*_prompts.py` 全部 | 2d | 3.1 |
| **3.3** | CitationParser 扩展：解析 `[local:xxx]` / `[ext:xxx]` / `[Agent 综合]` | `verifier.py` | 1d | 3.2 |
| **3.4** | 库内验证通道：local DB → chunk retrieval → claim-level semantic check | `verifier.py` | 2d | 3.3 |
| **3.5** | 外部验证通道：Crossref → arXiv → Semantic Scholar (fallback cascade) | `external_validator.py` | 2d | — |
| **3.6** | 验证结果缓存表 `external_validations` + Redis `external:validation:{doi}` | `db.py`, Redis | 0.5d | 3.5 |
| **3.7** | `output_verify` 节点接入 Main Agent StateGraph | `main_agent.py`, `main_graph.py` | 1.5d | 3.4, 3.5 |
| **3.8** | 验证失败处理：任一不通过 → 通知用户 + LLM 自动重生成（最多 2 次） | `main_agent.py` | 1d | 3.7 |
| **3.9** | 幻灭误事件记录到 `hallucination_events` 表 | `db.py`, `verifier.py` | 0.5d | 3.7 |
| **3.10** | 并行子 Agent 调度：`ParallelGroup` + `execute_plan` 改造 | `main_agent.py`, `reporter.py` | 2d | Phase 2 |
| **3.11** | 子 Agent 间短路径协作 + 共享上下文池 | `main_agent.py`, Redis | 1d | 3.10 |

### 5.3 验收标准

| # | 验收项 | 验证方式 |
|---|--------|----------|
| AC3.1 | RAG 回答中每句带引用标记 → 格式正确 | 自动化测试（10 条 query） |
| AC3.2 | 纯聊天回答中无强制引用标记 | 自动化测试（5 条 query） |
| AC3.3 | 库内文献引用验证通过：`[local:pap-xxx] ✓` 标记正确 | 自动化测试 |
| AC3.4 | 外部 DOI 引用通过 Crossref 验证：`[ext:10.xxx/xxx] ✓` | 自动化测试 |
| AC3.5 | 假 DOI 引用被标记为 `❌` 并触发自动重生成 | 自动化测试 |
| AC3.6 | 外部验证缓存生效：同一 DOI 二次验证命中 Redis 缓存 | 日志检查 |
| AC3.7 | hallucination_events 表有新记录（假引用触发后） | SQL 查询验证 |
| AC3.8 | Literature Agent + Knowledge Agent 并行执行（`depends_on=[]`）→ 均完成 | 自动化端到端测试 |
| AC3.9 | Literature Agent 结果自动传递到 Knowledge Agent（短路径协作） | 自动化端到端测试 |
| AC3.10 | 前端 ws 收到 `sub_agent/progress` 事件中包含并行进度信息 | 手动 WebSocket 监听 |

---

## 6. Phase 4：评估体系与收尾（Week 7-8）

### 6.1 目标

- 检索质量可量化评估（Recall@K / MRR / NDCG@K）
- 反幻觉自动化测试就绪
- 周度评估任务（Celery Beat）运行
- Zotero import/export tool 可用

### 6.2 任务列表

| # | 任务 | 文件 | 估时 | 依赖 |
|---|------|------|:---:|------|
| **4.1** | 检索质量测试集构建：3-5 个领域，每领域 10 条中文 query + 标注相关论文 | `test_sets/retrieval/` | 2d | Phase 2 |
| **4.2** | RecallEvaluator 实现（Recall@5/10/20, MRR, NDCG@10） | `agent/evaluator.py` | 1d | 4.1 |
| **4.3** | 评估流水线脚本：加载测试集 → 执行检索 → 计算指标 → 输出报告 | `scripts/run_evaluation.py` | 1d | 4.2 |
| **4.4** | 反幻觉红队测试集构建：20 条（假引用/假DOI/声明确认/跨论文混淆） | `test_sets/anti_hallucination/` | 1d | Phase 3 |
| **4.5** | 反幻觉自动化测试：逐条验证 → 统计准确率/拒答率 | `tests/anti_hallucination.py` | 1d | 4.4 |
| **4.6** | Celery Beat 周度定时评估任务 | `celery_tasks.py` | 0.5d | 4.3, 4.5 |
| **4.7** | 评估报告自动生成（Markdown） + 历史对比 | `scripts/generate_report.py` | 0.5d | 4.6 |
| **4.8** | Zotero import/export tool 整合进 tool_registry | `tool_registry.py` | 1d | — |
| **4.9** | 文档同步：product-spec / agent-manifest / CLAUDE.md | `docs/` | 1d | 4.1-4.8 |
| **4.10** | 集成测试 + 压力测试（10 并发用户） | 全部 | 1d | 4.1-4.8 |

### 6.3 验收标准

| # | 验收项 | 验证方式 |
|---|--------|----------|
| AC4.1 | Recall@10 ≥ 0.75（术语库后）、MRR ≥ 0.60、NDCG@10 ≥ 0.65 | `run_evaluation.py` 输出报告 |
| AC4.2 | 跨语言检索 Recall@10 对比中文直搜提升 ≥ 0.10 | 评估报告对比 |
| AC4.3 | 反幻觉测试通过率 ≥ 90%（假引用正确标记为 ❌） | 自动化测试 pass |
| AC4.4 | Celery Beat 周度任务自动执行 + 评估报告生成 | Celery 日志检查 |
| AC4.5 | Zotero import 成功导入论文到知识库 | 手动测试 |
| AC4.6 | Zotero export 输出正确 BibTeX 格式 | 手动验证 |
| AC4.7 | 10 并发用户同时入库不阻塞（响应时间 < 30s/pipeline） | 压力测试脚本 |
| AC4.8 | 重构后 iOS 客户端所有功能正常（全量回归） | 手动回归测试 |

---

## 7. 风险与依赖

### 7.1 技术风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|:---:|:---:|------|
| pgvector 检索性能不达预期 | 中 | 高 | Phase 1 即做性能基准测试，低于阈值切回 ChromaDB（双写模式） |
| doubao embedding 对中英混合 query 效果差 | 中 | 中 | Phase 4 评估阶段用 BGE-M3 等替代模型做对比 |
| Celery + Redis 运维复杂度 | 低 | 低 | Phase 2 提供 docker-compose 一键部署 |
| LLM 引用标记格式输出不稳定 | 中 | 中 | Phase 3 增加重试 + fallback 解析器（容错正则） |
| ingest_graph 拆分引入功能回归 | 中 | 高 | Phase 2 所有端到端测试覆盖旧版 ingest_graph 全流程 |

### 7.2 外部依赖

| 依赖 | 说明 | 状态 |
|------|------|:---:|
| PostgreSQL 16 + pgvector | 目标机器部署 | 需提前安装 |
| Redis 7 | 目标机器部署 | 需提前安装 |
| doubao / 火山引擎 API | LLM 调用 | 已有 |
| Crossref API | 外部引用验证 | 免费，无需 API Key |
| Semantic Scholar API | 外部引用验证 | 免费，已有 rate limit |
| arXiv API | 文献搜索 | 免费，已有集成 |
| CNKI | 中文文献搜索 | 需确认 API 可用性 |

### 7.3 关键里程碑

```
Week 1 结束 → Phase 1 完成：PostgreSQL 替换 SQLite，iOS 客户端正常
Week 3 结束 → Phase 2 完成：Agent 拆分完毕，术语词表后台运行
Week 5 结束 → Phase 3 完成：引用标记 + 验证通道正式上线
Week 7 结束 → Phase 4 完成：评估指标达标，全量回归通过
Week 8       → 缓冲周：性能调优 / Bug 修复 / 文档完善
```

---

> 验收细节见 [验收标准文档](./acceptance-criteria.md)。
