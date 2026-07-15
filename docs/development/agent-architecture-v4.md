# Paper Agent v4 — Agent 架构设计文档

> 最后更新: 2026-07-15
> 状态: 设计中（已确认部分 + 待讨论项）
> 来源: Claude Code 苏格拉底式架构对话

---

## 1. 概述

### 1.1 Agent 全景

```
Supervision Agent (原 MainAgent) — 编排层
  │
  ├── Literature Agent    — 搜索+下载+转换+评估+排名+批量
  ├── Knowledge Agent     — 分块+嵌入+去重+RAG+本地入库+发现
  ├── Research Agent      — 聚类+全景图+引用追踪
  ├── Writing Agent       — 综述+模板+引用检查+AI味检测+现状分析+缺口分析
  ├── Translation Agent   — 学术翻译+术语构建+术语丰富
  ├── Glossary Agent      — 术语提取+翻译+去重+入库+趋势
  └── Capture Agent       — 视频下载+转写+总结+分析
```

### 1.2 命名变更

| 旧名 | 新名 | 原因 |
|------|------|------|
| MainAgent / MainGraph | **Supervision Agent** | 更准确描述其编排监修角色 |
| ask_user node | **Gate** (统一模式) | 与 plan_review 合并 |
| plan_review node | **Gate** (统一模式) | 统一的人机交互 Gate |

### 1.3 关键设计原则

1. **Agent 级入口**：LLM 只需调一个 agent_ 工具完成完整流程，不再需要逐个调用细粒度工具
2. **只读优先**：clarify 阶段只允许只读工具，无副作用
3. **Gate 统一**：所有人机交互（plan审批、ask确认、clarify确认）走统一的 Gate 模式
4. **北京时间**：所有用户面时间戳强制北京时间（DB 仍存 UTC）
5. **Actor/Plan/Judge/Responder/Gate 五型分类**：按能力维度，非按流程位置

---

## 2. 节点类型分类体系

### 2.1 分类维度

| 维度 | 含义 | 可选值 |
|------|------|--------|
| ReAct | 是否支持多轮工具调用循环 | Yes / No |
| 工具范围 | 可调用的工具集合 | None / Restricted / Full |
| 模型 | LLM 模型选择 | Flash (快速) / Pro (深度) |
| Thinking | 是否启用思考链 | Enabled / Disabled |
| 输出格式 | LLM 输出类型 | Structured JSON (tool_choice) / Free Text |

### 2.2 五型分类

| 类型 | ReAct | 工具范围 | 模型 | Thinking | 输出 | 节点 |
|------|:---:|:---:|:---:|:---:|------|------|
| **Actor** | ✓ | Restricted / Full | Pro | Enabled | Free Text + tool_calls | execute, clarify |
| **Plan** | ✗ | None | Pro | Disabled | Structured JSON | plan |
| **Judge** | ✗ | None | Flash | Disabled | Structured JSON | fast_triage, intent_classify, ops_confirm, todo_checkpoint, evaluate |
| **Responder** | ✗ | None | Pro | Enabled | Free Text stream | inline_reply |
| **Gate** | ✗ | None | — | — | — | plan_review, ask_user (合并) |

### 2.3 工具范围分级

| 级别 | 工具数量 | 包含 | 节点 |
|:---:|:---:|------|------|
| **None** | 0 | 无 | Judge, Plan, Responder, Gate |
| **Restricted** | ~15 | 只读查询工具 (见 §5) | clarify |
| **Full** | ~13 agent_ + ~30 system | 全部注册工具 | execute |

---

## 3. Supervision Agent 流程

### 3.1 完整状态机

```
START
  │
  ▼
fast_triage (Judge)
  ├── chat ──→ inline_reply (Responder) → END
  ├── ops ───→ ops_confirm (Judge) → execute (Actor)
  └── research → intent_classify (Judge) → plan (Plan)
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                         clarify_needed   正常输出        needs_clarify
                              │               │               │
                              ▼               ▼               ▼
                          clarify (Actor)  Gate.plan_review  clarify (Actor)
                              │               │               │
                              │       ┌───────┼───────┐       │
                              │       ▼       ▼       ▼       │
                              │    approve  revise  timeout   │
                              │       │       │       │       │
                              │       ▼       ▼       ▼       │
                              │   execute  clarify  END       │
                              │   (Actor)  (Actor)            │
                              │       │       │               │
                              └───────┴───────┴───────────────┘
                                      │
                                      ▼
                              todo_checkpoint (Judge)
                                      │
                              ┌───────┼───────┐
                              ▼       ▼       ▼
                          satisfied retry  all_done
                              │       │       │
                              ▼       ▼       ▼
                          execute  execute  evaluate (Judge)
                          (next)   (retry)      │
                                      ┌────────┼────────┐
                                      ▼        ▼        ▼
                                    done   retry_tools ask_user
                                      │        │        │
                                      ▼        ▼        ▼
                                     END    execute  clarify
                                            (Actor)  (Actor)
```

### 3.2 clarify 节点两种模式

| 模式 | 触发条件 | 行为 |
|------|----------|------|
| `auto` | LLM 认为查询无风险、无隐私涉及 | 直接执行 ReAct，完成后回到 plan |
| `ask_first` | LLM 不确定或涉及外部搜索 | 先推 Gate 消息问用户"可以搜索吗？"，用户确认后执行 |

### 3.3 Plan → Clarify → Replan 闭环

```
plan 输出 clarify_needed: [{tool: "agent_knowledge_ask", args: {question: "find papers about..."}}]
  → clarify (auto) 执行 ReAct
  → 搜索结果写回 state.clarify_results
  → plan 重新生成（消费 clarify_results，不是从零开始）
  → Gate.plan_review
```

用户 revise 时同样：`plan_review → clarify → plan`，保留已确认的上下文。

---

## 4. 7 个子 Agent 详细定义

### 4.1 Literature Agent

**Graph**: `literature_graph.py`
**能力**: 搜索 → 评估 → 下载 → 转换 → 元数据提取 → 排名 → 批量搜索
**工具**: `agent_literature_search`

```
输入参数:
  - query: str          # 搜索查询
  - sources: list[str]  # 搜索源 (arxiv, semantic_scholar, etc.)
  - year_from: int      # 起始年份
  - max_results: int    # 最大结果数 (默认 20)
  - project_id: str     # 目标项目

输出:
  - papers: list[dict]  # 论文列表 (含 metadata, markdown_path, relevance_score)
  - total_found: int
  - downloaded: int

状态节点: search → evaluate → download → convert → extract_metadata
触发方式: LLM Plan → execute 节点 → agent_literature_search
依赖: PaperSearchEngine, PDFConverter, JournalRanker
```

### 4.2 Knowledge Agent

**Graph**: `knowledge_graph.py` (3 个子图)
**能力**: 分块+嵌入+去重+排名 / RAG 问答 / 本地 PDF 入库 / 论文全文查询
**工具**: `agent_knowledge_ingest`, `agent_knowledge_ask`, `agent_knowledge_ingest_local`

```
agent_knowledge_ingest:
  输入: project_id
  输出: indexed_count, ranked_count, errors
  子图: chunk → embed → dedup → rank

agent_knowledge_ask:
  输入: question, project_id?, top_k?, filter_paper_ids? [NEW]
  输出: answer, sources, confidence, follow_up_questions
  子图: parse → route → search → evaluate ⇄ refine → format
  [NEW] 支持 filter_paper_ids 限定论文范围
  [NEW] 支持 paper_id 精确查全文 (调用 search_fulltext)

agent_knowledge_ingest_local:
  输入: pdf_dir, project_id, agent_id?
  输出: indexed_count, fast_dup_count, figures_count, meta_quality
  8 Phase: 扫描→标题去重→转换→元数据提取→图表提取→向量去重→索引→归档

触发方式: LLM Plan → execute 节点
依赖: PgVectorStore, PostgresAgentDB, SectionChunker, RerankerClient, PDFConverter
```

### 4.3 Research Agent

**Graph**: `clustering_graph.py` + `citation_chase_graph.py`
**能力**: 聚类+全景图 / 引用追踪+发现
**工具**: `agent_clustering` [NEW], `agent_citation_chase` [迁移自 v1]

```
agent_clustering [NEW]:
  输入: project_id, n_clusters?
  输出: clusters, cluster_names, outliers, landscape_path
  节点: load → cluster → label → visualize → detect

agent_citation_chase [迁移]:
  输入: seed_title, seed_doi?, project_id?, max_depth?, direction?
  输出: layers_completed, total_found, total_ingested, report
  节点: resolve → check → fetch → filter → ingest → decide ⇄ loop → summarize

触发方式: LLM Plan → execute 节点
依赖: PgVectorStore (embeddings), PostgresAgentDB, SemanticScholar API, sklearn
```

### 4.4 Writing Agent

**Graph**: `writing_graph.py`
**能力**: 综述生成+模板+引用检查+AI味检测+**现状全景图+缺口分析** [NEW]
**工具**: `agent_generate_survey_v2`, `agent_check_ai_flavor`

```
agent_generate_survey_v2:
  输入: project_id, template?, language?
  输出: survey_content, citation_report, ai_flavor_report, 
        landscape_summary [NEW], gap_analysis [NEW]
  节点: survey → template_recommend → citation_format → ai_flavor_check
        → landscape [NEW] → gap_analysis [NEW]

  [NEW] landscape 节点:
    - 对项目论文聚类分析 → 研究分支地图
    - 每个分支的代表性工作 + 时间线
    - LLM 生成: "该领域的研究版图"

  [NEW] gap_analysis 节点:
    - 基于已入库论文做对比分析
    - 每个"缺口"声明附带证据来源
    - 输出: 覆盖不足的方向、方法缺口、数据/基准缺失、趋势断裂点
    - 标注 "AI辅助分析，需人工验证"
    - [待讨论] 能力边界：AI 不能替代人的文献阅读和创造性洞察

agent_check_ai_flavor:
  输入: text
  输出: score, flagged_patterns, cleaned_text

触发方式: LLM Plan → execute 节点
依赖: PostgresAgentDB, PgVectorStore, LLMClientV2
```

### 4.5 Translation Agent

**Graph**: `translation_graph.py`
**能力**: 学术翻译 / 术语表构建 / 术语丰富
**工具**: `agent_translate` [NEW, 统一入口]

```
agent_translate [NEW]:
  输入: action (translate_query|build_glossary|enrich), text?, project_id?, direction?
  输出: translation?, alternatives?, terms_added?, glossary_size?
  节点: route → translate|build|enrich

  注: 合并了旧工具 agent_translate_query + agent_build_glossary (v1)

触发方式: LLM Plan → execute 节点
依赖: PostgresAgentDB, LLMClientV2, PgVectorStore (术语检索)
```

### 4.6 Glossary Agent

**Graph**: `glossary_graph.py`
**能力**: 术语收集 → 翻译 → 去重 → 验证 → 趋势
**工具**: `agent_build_glossary_v2`

```
agent_build_glossary_v2:
  输入: project_id?, domain?
  输出: terms_collected, terms_verified, trend_report
  节点: collect → search → verify → evolve

  注: 与 Translation Agent 关系密切。Translation 负责"翻译一段文本中的术语"，
      Glossary 负责"从论文语料中系统性构建术语库"。
      两个 Agent 可互相调用：Translation 查 Glossary 获取已有翻译，
      Glossary 委托 Translation 做术语翻译。

触发方式: LLM Plan → execute 节点
依赖: PostgresAgentDB, PgVectorStore, LLMClientV2
```

### 4.7 Capture Agent

**Graph**: `video_graph.py`
**能力**: 链接解析 → 下载 → 转写 → 总结 → 分析
**工具**: `agent_capture_video`

```
agent_capture_video:
  输入: url, project_id?
  输出: title, summary, analysis, transcript_text
  节点: parse_link → fetch_metadata → download → extract_audio 
        → transcribe → summarize → analyze → notify

触发方式: LLM Plan → execute 节点
依赖: VideoDownloader (yt-dlp), faster-whisper, LLMClientV2
```

---

## 5. Clarify 受限工具集（15 个只读工具）

### 5.1 工具清单

| 类别 | 工具 | 用途 |
|------|------|------|
| **系统查询** | `read_file` | 读取文件内容 |
| | `glob_files` | 文件搜索/列表 |
| | `grep_content` | 文件内容搜索 |
| | `log_view` | 查看日志 |
| | `health_check` | 环境自检 |
| | `env_config` (read) | 读取配置 |
| | `get_current_time` [NEW] | 返回北京时间 |
| | `bash_query` [NEW] | 只读 shell 命令 |
| **网络查询** | `web_search` | 联网搜索 |
| | `web_fetch` | 抓取网页 |
| **知识查询** | `agent_knowledge_ask` | RAG 问答（已入库论文） |
| | `search_memory` | 搜索长期记忆 |
| | `list_collections` | 列出向量集合 |
| | `get_user_preference` | 读取用户偏好 |
| **论文查询** | `paper_status` | 项目/论文状态 |
| | `get_paper_abstract` | 论文摘要 |
| | `list_sources` | 搜索源列表 |
| | `list_subscriptions` | 订阅列表（只读） |

### 5.2 bash_query 允许的命令白名单

```
date, ls, cat, head, tail, wc, find, stat, du, df, 
who, ps, env, echo, which, uname, hostname, pwd
```

### 5.3 明确排除

- 所有 `agent_` 前缀工具（除 `agent_knowledge_ask`）
- 所有写操作：`write_file`, `edit_file`, `bash_exec`
- 所有记忆写操作：`summarize_memory`, `delete_memory`, `extract_to_long_term`, `tag_memory`, `update_preference`
- 所有订阅写操作：`create_subscription`, `delete_subscription`, `pause_subscription`, `resume_subscription`

---

## 6. Gate 统一模式

### 6.1 接口

```python
class GateMessage(BaseModel):
    gate_type: Literal["plan_review", "ask_user", "clarify_confirm"]
    message_id: str
    session_id: str
    payload: dict           # 具体内容（plan卡片 / ask问题 / clarify确认）
    timeout_seconds: int    # 默认 1800 (30min)
    priority_kind: str      # "high"
```

### 6.2 服务端行为

```
push GateMessage → BRPOP agent:ws:{id} → 
  匹配 type+session_id+message_id →
    plan_approve  → {approved: true}
    plan_revise   → {feedback: "..."}
    ask_reply     → {answer: "..."}
    clarify_ack   → {confirmed: true/false}
  超时 → 返回 None → Gate 超时处理
```

### 6.3 现有 ask_user 和 plan_review 的合并

- `_plan_review` 节点和 `_ask_user` 节点合并为一个 `_gate` 节点
- 通过 `gate_type` 字段区分行为
- 共享 parked queue 逻辑（已在 `daemon.py:_graph_get_user` 实现）

---

## 7. 旧工具清理方案

### 7.1 删除清单（A 方案：全部删除）

| # | 旧工具 | 替代 | 状态 |
|---|--------|------|:---:|
| 1 | `agent_search_papers` | `agent_literature_search` | 可删 |
| 2 | `agent_download_paper` | `agent_literature_search` | 可删 |
| 3 | `agent_convert_paper` | `agent_literature_search` | 可删 |
| 4 | `agent_evaluate_papers` | `agent_literature_search` | 可删 |
| 5 | `agent_rank_papers` | `agent_literature_search` | 可删 |
| 6 | `agent_batch_search` | `agent_literature_search` | 可删 |
| 7 | `agent_index_paper` | `agent_knowledge_ingest` | 可删 |
| 8 | `agent_search_library` | `agent_knowledge_ask` | 可删 |
| 9 | `agent_search_knowledge` | `agent_knowledge_ask` | 可删 |
| 10 | `agent_read_paper` | API 接口直接读 MD | 可删 |
| 11 | `agent_extract_knowledge` | `agent_knowledge_ask` (限定 paper_id) | 可删 |
| 12 | `agent_find_related` | → Literature Agent 定时推送 [NEW] | 迁移后删 |
| 13 | `agent_discover_gaps` | → Writing Agent landscape+gap_analysis | 迁移后删 |
| 14 | `agent_citation_chase` | → Research Agent `agent_citation_chase` | 迁移后删 |
| 15 | `agent_build_glossary` (v1) | `agent_build_glossary_v2` | 可删 |
| 16 | `agent_translate_query` (v1) | `agent_translate` [NEW] | 迁移后删 |
| 17 | `agent_generate_survey` (v1) | `agent_generate_survey_v2` | 可删 |
| 18 | `agent_paper_export` | `zotero_export` (非 agent_ 工具) | 可删 |
| 19 | `agent_paper_clean` | 保留为非 agent_ 工具 | 可删 |

### 7.2 废弃图文件删除

| 文件 | 原因 | 清理前需确认 |
|------|------|-------------|
| `ingest_graph.py` | 已拆分为 Literature + Knowledge | `routes.py` 仍 import 它 |
| `rad_query_graph.py` | 已合并到 KnowledgeAgent | `celery_tasks.py` 仍 import 它 |
| `history_graph.py` | 已合并到 LangGraph Store | 无外部引用 ✅ |

### 7.3 最终 agent_ 工具清单（13 个）

```
Literature Agent:     agent_literature_search
Knowledge Agent:      agent_knowledge_ingest, agent_knowledge_ask, agent_knowledge_ingest_local
Research Agent:       agent_clustering [NEW], agent_citation_chase
Writing Agent:        agent_generate_survey_v2, agent_check_ai_flavor
Translation Agent:    agent_translate [NEW]
Glossary Agent:       agent_build_glossary_v2
Capture Agent:        agent_capture_video
System (非 agent_):   约 45 个系统工具 (不变)
```

---

## 8. 反幻觉体系现状与缺口

### 8.1 已落地

| 层面 | 内容 | 文件 |
|------|------|------|
| L1 安全过滤 | 7 regex 模式 + LLM 二次确认 + fail-closed | `main_agent.py:_node_safety_filter` |
| L1 Schema 强约束 | 全部 LLM 调用 `chat_json(schema=PydanticModel)` + `tool_choice` | `llm_client_v2.py` |
| L2 引用验证 | CitationVerifier (提取→匹配→LLM 事实核查) | `verifier.py`, 集成于 `generate_report` |

### 8.2 已设计未落地

| 层面 | 内容 | 文档出处 | 缺失 |
|------|------|----------|------|
| L2 RAG 引用校验 | RAG 回答中的引用验证 | anti-hallucination.md §IV | 未集成到 KnowledgeAgent.ask() |
| L3 外部验证 | DOI/arXiv API 校验 | anti-hallucination.md §V | 模块存在但未集成 |
| L4 output_verify | 图中输出验证节点 | anti-hallucination.md §VI | 图中无此节点 |
| ANTI_FABRICATION | 8 个 prompt 的反虚构条款 | anti-hallucination.md §V.2 | 未添加到任何 prompt |
| hallucination_events | 幻觉审计日志 | init_db.sql 表已建 | 无 Python 代码写入 |
| groundedness judge | LLM 判断回答是否基于检索结果 | anti-hallucination.md §IV.5 | 无代码 |
| revise loop | 最多 2 轮修正 | anti-hallucination.md §VI.4 | 无代码 |

### 8.3 待办优先级

| 优先级 | 行动 | 工作量 |
|:---:|------|:---:|
| P0 | Writing Agent 综述输出集成 CitationVerifier | 2h |
| P0 | KnowledgeAgent.ask() 集成 CitationVerifier | 3h |
| P1 | 8 个 prompt 添加 ANTI_FABRICATION_CLAUSE | 1h |
| P1 | hallucination_events 表写入逻辑 | 2h |
| P2 | ExternalValidator 集成到 output_verify | 1d |
| P2 | groundedness LLM judge | 1d |
| P3 | revise loop | 2d |

---

## 9. 信息管理体系

### 9.1 三层能力现状

| 层次 | 能力 | 当前状态 | 缺口 |
|------|------|:---:|------|
| **碎片知识** | 对话摘要/记忆搜索/偏好学习/错误学习 | 🔶 v1→v2 迁移中 | memory.py v1 未完全清理；Store namespace 未全覆盖 |
| **结构化论文** | 22 列完整元数据/分块索引/期刊排名/图表元数据/归档管理 | ✅ | — |
| **进展追踪** | 关键词订阅+Beat+push/话题追踪/订阅结果 | 🔶 | 缺少语义关联发现；缺少 Literature Agent 定时推送相关论文 |

### 9.2 待补充能力

| 能力 | 归属 Agent | 说明 |
|------|-----------|------|
| 语义关联论文发现 | Literature Agent | 定时 Celery Beat → 对活跃 topic 语义搜索 → 推送相关新论文 |
| 研究进展推送 | Literature Agent + APNs | 订阅结果格式化后推送 iOS，含论文标题+摘要+相关性说明 |
| 碎片知识 v2 完成 | Memory + Store | 清理 memory.py v1 引用，Store 覆盖所有 namespace |

---

## 10. 时区处理方案

### 10.1 现状

- 服务器: `Asia/Shanghai (CST, +0800)` ✅
- 代码: 全部使用 `datetime.now(timezone.utc)` — DB 存 UTC ✅
- 用户面: 所有 timestamp 显示 UTC — ❌ 需改

### 10.2 方案

```python
# 新增 utils/datetime_utils.py
from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)

def beijing_now_iso() -> str:
    return beijing_now().isoformat()

def utc_to_beijing(utc_str: str) -> str:
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.astimezone(BEIJING_TZ).isoformat()
```

**修改范围**：
- 所有 Pydantic 模型 `created_at`/`updated_at`/`timestamp` 字段加 `@field_validator`
- `outbox.py` envelope timestamp → 北京时间
- `llm_client_v2.py` `当前日期` 系统提示 → 北京时间
- Celery Beat schedule → 北京时间（用户面）
- 不影响 DB 存储层（保持 UTC）

---

## 11. 文档产出清单

### 11.1 已确认需产出的文档

| 文档 | 文件名 | 内容 | 状态 |
|------|--------|------|:---:|
| Agent 架构设计 | `docs/development/agent-architecture-v4.md` | 本文档 | 🔶 初稿 |
| 反幻觉实现进度 | `docs/development/anti-hallucination.md` 末尾追加 | §8 内容 | 待写 |
| 升级优化/查缺补漏 | `docs/development/gap-analysis.md` [NEW] | §8+§9 内容 + 客观现状 | 待写 |
| 产品架构文档 | `docs/product/product-architecture-plan.md` | 已有，需对照更新 | 待更新 |

### 11.2 现有文档更新范围

- `CLAUDE.md`: 更新进度表、Agent 名、节点类型
- `docs/development/anti-hallucination.md`: 追加 §8 实现进度
- `docs/development/memory-system.md`: v2 迁移完成状态

---

## 12. 待讨论/待确认事项

### 12.1 clarify 节点

- [ ] `auto` vs `ask_first` 模式：由 LLM 在 ReAct 中自主决定，还是由 plan 节点输出字段控制？
- [ ] clarify 的超时策略：30 分钟？还是更短？
- [ ] clarify 结果如何注入 plan 的 replan prompt？是否需要专门的 `clarify_results` 格式化器？

### 12.2 bash_query 工具

- [ ] 是把 `bash_exec` 拆成两个工具（`bash_exec` + `bash_query`），还是在 `bash_exec` 内部根据节点上下文限制命令？
- [ ] 白名单命令是否需要支持参数化（如 `head -n 100`）——当前设计支持，但需确认

### 12.3 Plan 节点工具访问

- [ ] plan 是否应该返回 `clarify_needed` 字段（触发 clarify），而不是自己调工具？
- [ ] 如果 plan 需要工具调用来验证可行性（如"这篇论文是否已在库中"），应该返回 clarify_needed 还是直接调？

### 12.4 时区迁移

- [ ] Pydantic validator 是在序列化时自动转，还是需要显式调用 `beijing_now()`？
- [ ] Celery Beat schedule 是否也需要显示为北京时间（目前用 cron 表达式，无时区信息）？

### 12.5 旧代码清理顺序

- [ ] 先清理 `history_graph.py`（无外部引用，最安全）
- [ ] → `rad_query_graph.py`（需改 celery_tasks.py 路由到 KnowledgeAgent）
- [ ] → `ingest_graph.py`（需改 routes.py 路由到 LiteratureAgent）
- [ ] → 19 个旧 agent_ 工具（`_register_sub_agent_tools`）
- [ ] 清理顺序是否正确？是否需要分 PR？

### 12.6 Writing Agent landscape + gap_analysis

- [ ] gap_analysis 的输出格式：是综述末尾的一个章节，还是独立的结构化 JSON？
- [ ] 如何标注"AI辅助分析，需人工验证"？是通过 outbox `confidence` 字段还是文本内标记？
- [ ] 是否需要在 Gate 阶段让用户选择"是否包含缺口分析"（因为可能增加 LLM 调用成本）？

### 12.7 Knowledge Agent 增强

- [ ] `agent_knowledge_ask` 的 `filter_paper_ids` 参数：是否需要同时支持 `filter_project_id`、`filter_year`、`filter_venue`？
- [ ] 是否新增 `agent_knowledge_get_fulltext` 工具（读取某篇论文完整 MD），还是通过 `agent_knowledge_ask` + `filter_paper_ids` 覆盖？

### 12.8 Literature Agent 定时推送

- [ ] 语义关联发现的调度频率？每小时？每天？
- [ ] 是否复用现有 `subscription_check_task`，还是新增 Celery task？
- [ ] 推送内容格式：纯文本 vs 结构化 JSON（含 paper title/authors/abstract/relevance_score）？
