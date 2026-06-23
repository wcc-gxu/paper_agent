# PlanGraph 硬编码 17 场景路由表 + C 档缺口实现计划

> 状态：设计稿（2026-06-23）
> 关联：[main-agent.md](main-agent.md) · [anti-hallucination.md](anti-hallucination.md)
> 范围：仅设计，不改代码。落地实施需另开 PR。

---

## 0. 背景与问题

当前 `scenario_plan` 节点让 LLM 生成完整 `tools[]`（每个 `ToolCallSpec` 含 `kind/name/arguments/depends_on`）。但代码查证显示：

1. **17 场景执行全是固定流水线**，LLM 只在填参数（且经常填错 / 幻觉 tool name）。
2. **`sub_agent_task` 硬编码 ingest**（`celery_tasks.py:571-580` 签名无 `agent_type`），`_handle_sub_agent`（`main_agent.py:1028-1033`）读 `agent_type` 却不传递 → S6/S8/S9/S12/S13 全部落入 ingest 流水线（P0 bug）。
3. **6 个 C 档场景"无执行体"是假象**：clustering / citation_chase / translation / video 四个 graph 已完整实现，只是没接进 dispatch；S3 缺一个 `create_subscription` 工具；S6 的 `discover_gaps` 是 stub 但 `ClusteringAgent._detect_node` 已实现新方向检测。
4. **11 个子 Agent 工具是 stub**（`tool_registry.py` 中 `download_paper`/`convert_paper`/`index_paper`/`evaluate_papers`/`rank_papers`/`generate_survey`/`paper_export`/`batch_search`/`citation_chase`/`extract_knowledge`/`find_related`/`discover_gaps`/`build_glossary`/`translate_query` 都只返回 "dispatched" 字符串，未真正调度）。

**瘦身后**：`scenario_plan` 的 LLM 只判 `{scenarios, clarity, clarify_questions?}`，**不再生成 tools[]**。`tools[]` 由 PlanGraph 按 `scenario_id` 硬编码路由表展开。

---

## 1. 设计原则

### 1.1 职责重划

| 层 | 改造前 | 改造后 |
|---|---|---|
| `intent_classify` LLM | 判 intent_kind + scenarios list | **不变** |
| `scenario_plan` LLM | 生成 summary + tools[] + permissions + approval + clarify | **只判** `{scenarios, clarity, clarify_questions?}`（信息充足度 + 澄清问题） |
| PlanGraph（新增） | 不存在 | 按 `scenario_id` 查路由表 → 展开固定 `tools[]` + 抽参数 + 跨场景依赖编排 |
| `evaluate_completion` LLM | 判 satisfied + 生成 needs_more_tools[] | 判 satisfied（`needs_more_tools` 改由 PlanGraph 重展开，LLM 只说"还差什么"） |

### 1.2 三条硬规则

1. **scenario_id → 执行体是查表，不是生成**。PlanGraph 维护一张 `SCENARIO_ROUTING` 常量表，`scenario_id` 映射到固定的 `kind + name + 默认参数 + danger_level`。LLM 永远不决定调哪个工具。
2. **参数抽取是确定性代码，不是 LLM**。每个 scenario 有专属 `_extract_params_S1(user_msg, history) → dict`，用正则 / 简单规则抽（query 文本、max_results 数字、paper_id、url 等），抽不到用默认值。LLM 只判"信息够不够"（clarity）。
3. **danger_level 硬映射 permissions**。`low` → 直接执行；`medium` → 推 propose_plan 卡片让用户确认；`high` → 强制 approval + 审计日志。不再让 LLM 判 `needs_approval`。

### 1.3 PlanGraph 职责边界

PlanGraph **只做**：
- 接收 `scenarios list` + 用户消息 + history
- 逐场景查路由表 → 生成 `ToolCallSpec`（含 `call_id` / `kind` / `name` / `arguments` / `depends_on`）
- 跨场景依赖编排（复合意图时按 `depends_on` 串/并行）
- 合并所有场景的 `tools[]` + `permissions_required` + `estimated_time_seconds` + `needs_approval`（按 danger_level 算）

PlanGraph **不做**：LLM 调用、澄清循环、用户交互（这些留在 `scenario_plan` 节点）。

---

## 2. 17 场景路由表

### 2.1 总览

| ID | name | 执行体 | kind | danger_level | 档 | 当前状态 |
|---|---|---|---|---|---|---|
| S1 | 文献调研 | `search_papers` tool | tool | low | A | ✅ 可用 |
| S2 | 综述生成 | `ingest` sub_agent | sub_agent | medium | A | ✅ 可用 |
| S3 | 前沿追踪 | `create_subscription` tool (新) | tool | low | C | ❌ 缺工具 |
| S4 | 论文精读 | `read_paper` + `extract_knowledge` | tool | low | B | 🔶 extract_knowledge stub |
| S5 | 方法对比 | `search_papers` x2 + LLM compare | tool | low | B | 🔶 无 compare 工具 |
| S6 | 研究空白 | `clustering` sub_agent | sub_agent | low | C | ❌ dispatch bug |
| S7 | 进度查看 | `paper_status` tool | tool | low | A | ✅ 可用 |
| S8 | 聚类全景 | `clustering` sub_agent | sub_agent | low | C | ❌ dispatch bug |
| S9 | 引用追溯 | `citation_chase` sub_agent | sub_agent | low | C | ❌ dispatch bug |
| S10 | RAG 问答 | `rad_query` sub_agent | sub_agent | low | B | 🔶 graph 待接 |
| S11 | 批量搜索 | `batch_search` tool | tool | medium | B | 🔶 stub |
| S12 | 翻译 | `translation` sub_agent | sub_agent | low | C | ❌ dispatch bug |
| S13 | 视频解析 | `video` sub_agent | sub_agent | high | C | ❌ dispatch bug |
| S14 | 导出/清理 | `paper_export` / `paper_clean` | tool | low/high | B | 🔶 export stub |
| S15 | iOS 自动化 | `ios_*` tools | ios_tool | low | A | ✅ 可用 |
| S16 | 运维操作 | `service_*` / `bash_exec` / `pip_install` | tool | high | A | ✅ 可用 |
| S17 | 记忆操作 | `search_memory` / `get_user_preference` / `extract_to_long_term` | tool | low | A | ✅ 可用 |

### 2.2 逐场景详表

#### S1 文献调研 / 筛选

- **执行体**：`tool` → `search_papers`
- **抽取参数**：`query`(必填，从用户消息取主题词), `sources`(默认 `["arxiv","semantic_scholar"]`), `year_from`(默认 2022), `max_results`(默认 20)
- **danger_level**：low（permissions=search，只读）
- **档/状态**：A / `search_papers` 已真实调用 `PaperSearchEngine.search`（`tool_registry.py:945-970`）
- **复合依赖**：常与 S12（翻译标题）、S8（聚类）、S14（导出）组合 → S1 是数据源，下游 `depends_on` S1 的 `call_id`

#### S2 文献综述生成

- **执行体**：`sub_agent` → `ingest`（7-8 阶段：search→evaluate→download→convert→index→rank→[verify]→survey）
- **抽取参数**：`query`(必填), `sources`(默认全源), `year_from`(默认 2022), `max_results`(默认 **50**，综述需更大语料), `enable_verify`(默认 false，可由用户"严格校验"触发)
- **danger_level**：medium（permissions=search+download，下载占带宽/磁盘）
- **档/状态**：A / `IngestAgent` + `PipelineRunner` + `sub_agent_task` 全链路可用
- **复合依赖**：S2 产出 `project_id` + `survey_path`，S12 可对 survey 做翻译

#### S3 每日前沿追踪（订阅）

- **执行体**：`tool` → `create_subscription`（**新建**）
- **抽取参数**：`keywords`(必填), `name`(可选，默认取 keywords 前 20 字), `sources`(默认 `["arxiv","semantic_scholar"]`)
- **danger_level**：low（创建订阅记录是写 SQLite，但不动论文库）
- **档/状态**：C / `subscription_check_task` 已存在且 Beat 已调度（`celery_tasks.py:772`），但**无创建订阅的入口工具**。用户说"订阅扩散模型"目前无路可走。
- **复合依赖**：S3 创建后由 Beat 异步触发 `subscription_check_task` → 命中新论文走 S1 路径入库 → APNs 推送

#### S4 论文精读 / 提炼

- **执行体**：`tool` → `read_paper`（读 Markdown 全文）+ `extract_knowledge`（结构化提取）
- **抽取参数**：`paper_id` 或 `title`(必填，从消息抽论文标题/ID), `deep`(默认 false)
- **danger_level**：low（只读）
- **档/状态**：B / `read_paper` 真实可用（`tool_registry.py:1054-1064`）；`extract_knowledge` 是 stub（返回 "dispatched" 字符串）。需补真实实现：调 LLM 对 markdown 做 method/contribution/limitation 三段式提取。
- **复合依赖**：常单独触发；可与 S17（extract_to_long_term）串接把精读结果存长期记忆

#### S5 方法对比

- **执行体**：`tool` → `search_papers`(查 A) + `search_papers`(查 B) + LLM compare（**需新增 `compare_methods` 工具** 或在 evaluate_completion 里让 LLM 综合）
- **抽取参数**：`method_a`, `method_b`(必填，从"对比 X 和 Y"模式抽), `sources`, `max_results`(默认 10 each)
- **danger_level**：low
- **档/状态**：B / 无专用 compare 工具。当前路由到 ingest 是错的（ingest 会下载全文，过重）。设计：并行两次 `search_papers` → LLM 在 evaluate 节点综合对比。
- **复合依赖**：两次 search 无依赖可并行；compare 依赖两个 search 的 call_id

#### S6 研究空白分析

- **执行体**：`sub_agent` → `clustering`（取 `new_directions` 输出）
- **抽取参数**：`project_id`(必填，从 history 或当前 project 取), `domain`(可选，LLM 用于聚焦)
- **danger_level**：low（只读已入库语料）
- **档/状态**：C / `ClusteringAgent._detect_node` 已实现 HDBSCAN outlier + LLM 新方向评估（`clustering_graph.py:244-332`），但 `sub_agent_task` 不 dispatch 到 clustering。
- **复合依赖**：通常 S6 依赖前置的 S1/S2 已入库语料；可串 S12 翻译新方向标签

#### S7 进度查看

- **执行体**：`tool` → `paper_status`
- **抽取参数**：`project_id` 或 `paper_id`(可选，无则列最近项目)
- **danger_level**：low
- **档/状态**：A / `paper_status` 真实可用（`tool_registry.py:875-889`）
- **复合依赖**：无

#### S8 研究方向聚类 + 全景图

- **执行体**：`sub_agent` → `clustering`（取 `clusters` + `landscape_path` 输出）
- **抽取参数**：`project_id`(必填), `n_clusters`(默认 0=auto)
- **danger_level**：low
- **档/状态**：C / `ClusteringAgent` graph 完整（load→cluster→label→visualize→detect），`landscape.json` 输出 t-SNE 2D 坐标。dispatch bug 阻断。
- **复合依赖**：S8 与 S6 共享 clustering graph，差别在取不同输出字段；S8 产出 `landscape_path` 可被 S14 导出

#### S9 引用追溯

- **执行体**：`sub_agent` → `citation_chase`
- **抽取参数**：`seed_title` 或 `seed_doi` 或 `paper_id`(必填), `max_depth`(默认 2), `direction`(默认 `both`)
- **danger_level**：low（Semantic Scholar API 只读，1 req/s 限速）
- **档/状态**：C / `CitationChaseAgent` graph 完整（resolve→check→fetch→filter→ingest→decide loop→summarize），dispatch bug 阻断。
- **复合依赖**：S9 入库的新论文可触发 S12 翻译标题；S9 依赖 S1/S2 提供种子论文 project_id

#### S10 RAG 问答（已入库）

- **执行体**：`sub_agent` → `rad_query`
- **抽取参数**：`question`(必填), `project_id`(可选), `top_k`(默认 5), `use_fulltext`(默认 true)
- **danger_level**：low
- **档/状态**：B / `RADQueryAgent` graph 完整（parse→route→search→evaluate refine→format），`search_library` tool 真实可用。但 `_handle_sub_agent` 不 dispatch 到 rad_query。
- **复合依赖**：S10 依赖 S1/S2 已入库语料；无下游

#### S11 批量搜索

- **执行体**：`tool` → `batch_search`（需补真实实现：读 csv/json → 逐行 `search_papers` → 汇总）
- **抽取参数**：`file_path`(必填，从消息抽附件路径或用户提供的路径), `download`(默认 false)
- **danger_level**：medium（若 download=true 则下载）
- **档/状态**：B / `batch_search` 是 stub（`tool_registry.py:1021-1024`）。设计：实现为循环调 `search_papers`，或 dispatch 一个轻量 Celery 编排器。
- **复合依赖**：S11 产出多 project_id，可串 S14 导出

#### S12 学术翻译 / 术语库

- **执行体**：`sub_agent` → `translation`（action 路由：translate_query / build_glossary / enrich）
- **抽取参数**：`text`(翻译输入) 或 `action`(默认 `translate_query`; 用户说"建术语库"→ `build_glossary`), `direction`(默认 `zh2en`), `project_id`(build_glossary 必填)
- **danger_level**：low
- **档/状态**：C / `TranslationAgent` graph 完整 + 有直接调用方法（`translate_query()`/`build_glossary()`/`enrich_terminology()`），dispatch bug 阻断。
- **复合依赖**：S12 常作为下游：S1+S12（搜+翻译标题）、S2+S12（综述+翻译）、S9+S12（追溯+翻译）

#### S13 视频解析

- **执行体**：`sub_agent` → `video`（8 阶段：parse_link→fetch_metadata→download→extract_audio→transcribe→summarize→analyze→notify）
- **抽取参数**：`url`(必填，从消息抽视频分享链接/口令), `project_id`(默认生成新 task_id)
- **danger_level**：**high**（permissions=video_download，yt-dlp 下载 + ffmpeg + whisper 占资源）
- **档/状态**：C / `VideoAgent` graph 完整，dispatch bug 阻断。额外依赖：`yt-dlp` / `faster-whisper` / `ffmpeg` 需安装（`pip install -e ".[video]"` + `apt install ffmpeg`）。
- **复合依赖**：S13 产出 `summary` + `analysis`，可串 S17 存长期记忆

#### S14 导出 / 清理

- **执行体**：`tool` → `paper_export`（导出）或 `paper_clean`（清理）
- **抽取参数**：`action`(export|clean，从消息判), `project_id`(必填), `format`(默认 `bibtex`), `keep_pdfs`(默认 true, clean 时)
- **danger_level**：export=low；**clean=high**（删 DB 记录，需 approval）
- **档/状态**：B / `paper_clean` 真实可用（`tool_registry.py:1012-1019`）；`paper_export` 是 stub。需补 BibTeX/JSON 导出实现。
- **复合依赖**：S14 常作为收尾：S1+S14（搜完导出）、S2+S14（综述完导出引用列表）

#### S15 iOS 自动化

- **执行体**：`ios_tool` → `ios_calendar_add` / `ios_reminder_add` / `ios_file_*` / `ios_notification_local` / `ios_device_info` / `ios_location_get`
- **抽取参数**：`tool_name`(从消息意图判，如"加日历"→`ios_calendar_add`), 对应工具参数（title/start_time 等）
- **danger_level**：low（iOS 端自管系统权限）
- **档/状态**：A / 9 个 ios_tool 已注册，走 WS round-trip（`_handle_ios_tool`）
- **复合依赖**：无

#### S16 运维操作

- **执行体**：`tool` → `service_start`/`service_stop`/`service_status`/`docker_compose_*`/`apt_install`/`pip_install`/`bash_exec`/`env_config`/`log_view`/`health_check`
- **抽取参数**：`tool_name`(从消息判), 对应参数（service name / command / packages）
- **danger_level**：**high**（shell_exec / package_install 强制 approval + 审计）
- **档/状态**：A / 10 个运维工具已注册
- **复合依赖**：无

#### S17 记忆操作

- **执行体**：`tool` → `search_memory` / `get_user_preference` / `extract_to_long_term` / `summarize_memory` / `delete_memory` / `tag_memory` / `list_collections`
- **抽取参数**：`tool_name`(从消息判), `query`/`key`/`content_json` 等
- **danger_level**：low（`delete_memory` 可提 medium，但默认 low）
- **档/状态**：A / 7 个记忆工具已注册且有真实实现
- **复合依赖**：S17 常作为收尾把上游结果存记忆：S4+S17（精读+存长期）、S13+S17（视频摘要+存）

### 2.3 danger_level → needs_approval 硬映射

```python
DANGER_TO_APPROVAL = {
    "low":    {"needs_approval": False, "priority_kind": "normal"},
    "medium": {"needs_approval": True,  "priority_kind": "high"},   # 推 propose_plan 卡片
    "high":   {"needs_approval": True,  "priority_kind": "high", "audit": True},
}
```

场景级默认 danger_level（复合意图取 max）：

| danger_level | 场景 |
|---|---|
| low | S1, S3, S4, S5, S6, S7, S8, S9, S10, S12, S15, S17 |
| medium | S2, S11(download=true) |
| high | S13, S14(clean), S16 |

---

## 3. C 档实现计划（6 个缺口）

### 3.0 P0 前置修复：`sub_agent_task` 支持 `agent_type` 分发

**这是 6 个 C 档缺口的公共前置**。当前 `sub_agent_task`（`celery_tasks.py:571-580`）签名无 `agent_type`，硬编码跑 ingest 7 阶段。

**修复点**：

1. `sub_agent_task` 签名加 `agent_type: str = "ingest"`
2. 函数体内按 `agent_type` 分支：
   - `ingest` → 现有 7 阶段逻辑
   - `clustering` → 实例化 `ClusteringAgent(db, chroma, llm).compile()` → `ainvoke(state)`
   - `citation_chase` → 实例化 `CitationChaseAgent(db, llm, engine).compile()` → `ainvoke(state)`
   - `translation` → 实例化 `TranslationAgent(db, llm, chroma).compile()` → `ainvoke(state)`
   - `video` → 实例化 `VideoAgent(downloader, whisper, llm, db, videos_dir).compile()` → `ainvoke(state)`
   - `rad_query` → 实例化 `RADQueryAgent(kb).compile()` → `ainvoke(state)`
3. `_handle_sub_agent`（`main_agent.py:1028-1033`）调 `sub_agent_task.delay(...)` 时传 `agent_type=spec.name`
4. lifecycle 上报改用对应 `agent_type`（当前硬编码 `"ingest"`）

**工作量**：中（约 200-300 行，主要是 6 个 graph 的依赖注入 + state 构造）。每个 graph 的依赖（engine/db/llm/chroma/converter/ranker/downloader/whisper）需在 Celery worker 进程内惰性构造。

**文件**：`src/paper_search/agent/celery_tasks.py`（sub_agent_task 改造）+ `src/paper_search/agent/main_agent.py`（_handle_sub_agent 传参）

---

### 3.1 S3 subscription — 新建 `create_subscription` 工具

- **缺口**：`subscription_check_task` 已跑（Beat 每 60 分钟），但用户无法创建订阅。
- **所需工具**：`create_subscription(keywords, name, sources)` → 写 `subscriptions` 表（`db.create_subscription()`，需确认 AgentDB 有此方法）
- **graph 文件**：无（纯 tool）
- **入口函数**：`ToolRegistry._register_create_subscription()`（新增）
- **所需参数**：`keywords`(必填), `name`(可选), `sources`(默认 `["arxiv","semantic_scholar"]`)
- **额外工作**：
  - 确认 `AgentDB` 有 `create_subscription` / `list_subscriptions` / `update_subscription` / `save_subscription_result` 方法（`subscription_check_task` 已调用后三个，第一个需补）
  - 注册到 `ToolRegistry._register_all()`
- **工作量**：**小**（~50 行：工具注册 + DB 方法补全）

---

### 3.2 S6 / S8 clustering — P0 修复后接 dispatch

- **缺口**：graph 完整，dispatch bug 阻断。
- **graph 文件**：`src/paper_search/agent/graphs/clustering_graph.py`
- **入口函数**：`ClusteringAgent(db, chroma_store, llm, on_progress).compile()` → `graph.ainvoke({"project_id": ..., "n_clusters": ...})`
- **所需参数**：`project_id`(必填), `n_clusters`(默认 0=auto)
- **内部实现完整度**：✅ 完整
  - `_load_node`：从 DB + ChromaDB 加载论文 embedding
  - `_cluster_node`：K-means（sklearn）+ 自动聚类数
  - `_label_node`：LLM 为每聚类命名
  - `_visualize_node`：t-SNE 降维 → `landscape.json`
  - `_detect_node`：HDBSCAN outlier + LLM 评估新方向 → `clusters.json`
  - **依赖**：`sklearn`（KMeans/TSNE/HDBSCAN）、`numpy`
- **S6 vs S8 差异**：同一个 graph，取不同输出字段
  - S6（研究空白）→ 取 `result.new_directions` + `result.outliers`
  - S8（聚类全景）→ 取 `result.cluster_names` + `landscape_path`
- **工作量**：**小**（P0 修复后零代码，仅 dispatch 分支 + 结果字段映射）

---

### 3.3 S9 citation_chase — P0 修复后接 dispatch

- **缺口**：graph 完整，dispatch bug 阻断。
- **graph 文件**：`src/paper_search/agent/graphs/citation_chase_graph.py`
- **入口函数**：`CitationChaseAgent(db, llm, engine, runner=None, on_progress).compile()` → `graph.ainvoke({"seed_title": ..., "project_id": ..., "max_depth": 2, "direction": "both"})`
- **所需参数**：`seed_title` 或 `seed_doi`(必填), `project_id`(必填), `max_depth`(默认 2), `direction`(默认 `both`)
- **内部实现完整度**：✅ 完整
  - `_resolve_node`：DB 查种子论文
  - `_fetch_node`：调 `SemanticScholarProvider.search_citations()`
  - `_filter_node`：LLM 评估相关性（`llm.evaluate_relevance`）
  - `_ingest_node`：写 DB + link to project
  - `_decide_node`：LLM 决定是否继续下一层（loop）
  - `_summarize_node`：生成 `citation_chase_report.md`
  - **依赖**：`SemanticScholarProvider`（已有）、`SEMANTIC_SCHOLAR_API_KEY`（1 req/s 限速）
- **工作量**：**小**（P0 修复后零代码）

---

### 3.4 S12 translation — P0 修复后接 dispatch

- **缺口**：graph 完整，dispatch bug 阻断。
- **graph 文件**：`src/paper_search/agent/graphs/translation_graph.py`
- **入口函数**：`TranslationAgent(db, llm, chroma_store, on_progress).compile()` → `graph.ainvoke({"action": "translate_query", "text": ..., "direction": "zh2en"})`
  - 或直接调 `agent.translate_query(text)` / `agent.build_glossary(project_id)` / `agent.enrich_terminology(project_id)`（绕过 graph）
- **所需参数**：
  - translate_query：`text`(必填), `direction`(默认 `zh2en`)
  - build_glossary：`project_id`(必填)
  - enrich：`project_id`(必填)
- **内部实现完整度**：✅ 完整
  - `_translate_node`：先查 `terminology` 表 → LLM 翻译 → 返回 translation + alternatives + keywords
  - `_build_node`：遍历项目论文 → LLM 提取中英术语 → 写 `terminology` 表 + ChromaDB
  - `_enrich_node`：找未提取术语的论文 → 复用 `_build_node`
  - **依赖**：`terminology` 表（需确认 DB schema 有此表）
- **工作量**：**小**（P0 修复后零代码）

---

### 3.5 S13 video — P0 修复后接 dispatch + 依赖确认

- **缺口**：graph 完整，dispatch bug 阻断 + 运行时依赖需装。
- **graph 文件**：`src/paper_search/agent/graphs/video_graph.py`
- **入口函数**：`VideoAgent(downloader, whisper_model, llm, db, videos_dir, on_progress).compile()` → `graph.ainvoke({"project_id": ..., "user_query": "<视频链接>"})`
- **所需参数**：`user_query`(必填，含视频链接/口令), `project_id`(默认生成新 task_id)
- **内部实现完整度**：✅ 完整
  - `_parse_link_node`：`parse_link()` + `detect_platform()`（`video_downloader.py`）
  - `_fetch_metadata_node`：`downloader.extract_info(url)`（yt-dlp `--dump-json`）
  - `_download_video_node`：`downloader.download_video(url)`
  - `_extract_audio_node`：`downloader.extract_audio(video_path)`（ffmpeg）
  - `_transcribe_node`：`whisper.transcribe()`（faster-whisper，>10 分钟跳过）
  - `_summarize_node`：LLM 结构化摘要（one_line_summary / key_points / core_thesis / tags）
  - `_analyze_node`：LLM 深度分析（stance / logic_chain / factual_claims / fact-check）
  - `_notify_node`：`db.save_video_result()` + 写 transcript 文件
- **运行时依赖**：
  - `yt-dlp>=2024.12`（视频下载）
  - `faster-whisper>=1.1.0`（语音识别）
  - `ffmpeg`（音频提取，系统命令）
  - `cloakbrowser>=0.3`（某些平台需浏览器解壳，可选）
  - 安装：`pip install -e ".[video]"` + `sudo apt install ffmpeg`
  - 环境变量：`WHISPER_MODEL_SIZE`（默认 `small`）、`CLOAKBROWSER_HEADLESS`
- **工作量**：**小**（P0 修复后零代码）+ 依赖安装（运维，非编码）

---

### 3.6 C 档工作量汇总

| 缺口 | 依赖 P0 | 编码工作 | 运维工作 | 总工作量 |
|---|---|---|---|---|
| P0 dispatch 修复 | — | 中（200-300 行） | — | 中 |
| S3 create_subscription | 无 | 小（~50 行） | — | 小 |
| S6 clustering | P0 | 零（graph 已完整） | — | 小 |
| S8 clustering | P0 | 零（同 S6） | — | 小 |
| S9 citation_chase | P0 | 零（graph 已完整） | — | 小 |
| S12 translation | P0 | 零（graph 已完整） | — | 小 |
| S13 video | P0 | 零（graph 已完整） | 装 yt-dlp/whisper/ffmpeg | 小 |

**结论**：C 档 6 个缺口中，5 个（S6/S8/S9/S12/S13）在 P0 修复后立即可用（graph 均已完整实现）。真正的编码工作集中在 P0 dispatch 修复（中）+ S3 新工具（小）。**总工作量：中**。

---

## 4. 复合意图编排

### 4.1 依赖表达

跨场景依赖用 `ToolCallSpec.depends_on`（`call_id` 列表）表达。PlanGraph 合并多场景 tools[] 时：

- **同场景内**：路由表预定义 `depends_on`（如 S2 的 download depends_on search）
- **跨场景**：PlanGraph 在合并时自动添加跨场景 `depends_on`。规则：若场景 B 的工具需要场景 A 的输出（如 S12 翻译需要 S1 的论文列表），则 B 的首个 tool `depends_on` A 的末个 tool 的 `call_id`。

### 4.2 典型组合

| 组合 | 依赖关系 | 并行/串行 |
|---|---|---|
| **S1 + S12**（搜+翻译标题） | S12.translate depends_on S1.search_papers | 串行 |
| **S2 + S12**（综述+翻译） | S12.translate depends_on S2.ingest(survey) | 串行（S2 耗时长） |
| **S1 + S8**（搜+聚类） | S8.clustering depends_on S1.search_papers | 串行（S8 需语料） |
| **S1 + S14**（搜+导出） | S14.paper_export depends_on S1.search_papers | 串行 |
| **S5 = search A + search B + compare** | 两个 search 无依赖 → 并行；compare depends_on 两个 search | 前并行 + 后串行 |
| **S2 + S17**（综述+存记忆） | S17.extract_to_long_term depends_on S2.ingest | 串行 |
| **S13 + S17**（视频+存记忆） | S17 depends_on S13.video | 串行 |
| **S9 + S12**（追溯+翻译） | S12 depends_on S9.citation_chase | 串行 |
| **S1 + S12 + S14**（搜+翻译+导出） | S1 → S12 → S14 全串行 | 串行链 |

### 4.3 并行 vs 串行判定

PlanGraph 用拓扑排序（`_node_execute_plan` 已有此逻辑，`main_agent.py:953-984`）：

```python
# 伪代码
def merge_tools(scenarios, user_msg, history):
    all_tools = []
    cross_scenario_deps = {}  # {scenario_id: last_call_id}
    for sm in scenarios:
        sid = sm.scenario_id
        scenario_tools = ROUTING[sid].expand(user_msg, history)  # 查表展开
        # 跨场景依赖：若 sid 在 COMPOUND_DEPS 中有上游，给首个 tool 加 depends_on
        upstream = COMPOUND_DEPS.get(sid, [])
        for up_sid in upstream:
            if up_sid in cross_scenario_deps:
                scenario_tools[0].depends_on.append(cross_scenario_deps[up_sid])
        all_tools.extend(scenario_tools)
        cross_scenario_deps[sid] = scenario_tools[-1].call_id
    return all_tools
```

**判定规则**：
- **无依赖** → 并行（`asyncio.gather`，`_node_execute_plan` 已实现分批并行）
- **有依赖** → 串行（`depends_on` 非空的工具等上游 completed）

**复合意图的 COMPOUND_DEPS 映射**（硬编码）：

```python
COMPOUND_DEPS = {
    "S12": ["S1", "S2", "S9"],   # 翻译常依赖上游产出论文/综述/追溯结果
    "S8": ["S1", "S2"],           # 聚类依赖已入库语料
    "S6": ["S1", "S2"],           # 研究空白依赖语料
    "S14": ["S1", "S2", "S9"],    # 导出依赖有数据
    "S17": ["S2", "S4", "S13"],   # 存记忆依赖上游产出
}
# 仅当上游场景也在当前 scenarios list 中时才加 depends_on
```

---

## 5. 与 scenario_plan 节点的接口

### 5.1 瘦身后的 `ScenarioPlanResult` schema

```python
class ScenarioPlanResult(BaseModel):
    """瘦身后：只判场景确认 + 信息充足度 + 澄清问题。不生成 tools[]。"""

    scenarios: list[ScenarioMatch] = Field(
        ..., description="确认的场景列表（可能过滤掉 intent_classify 给的低置信项）"
    )
    clarity: float = Field(
        ..., ge=0.0, le=1.0,
        description="信息充足度。>=0.6 直接执行；<0.6 触发 clarify_questions",
    )
    clarify_questions: list[ClarificationQuestion] = Field(
        default_factory=list,
        description="clarity < 0.6 时非空；每个问题绑定到具体 scenario_id",
    )
    # ── 删除的字段 ──
    # summary: str               → 由 PlanGraph 按路由表模板生成
    # needs_clarification: bool  → 由 clarity < 0.6 推导
    # needs_approval: bool       → 由 danger_level 硬映射
    # permissions_required: list → 由路由表查
    # estimated_time_seconds: int→ 由路由表查
    # tools: list[ToolCallSpec]  → 由 PlanGraph 展开
```

### 5.2 节点流转（瘦身后）

```
intent_classify (LLM)
  → IntentClassifyResult {intent_kind, scenarios[], overall_confidence}
  → C3 灰区处理（confidence < 阈值 → ask_user 挑选）
  ↓
scenario_plan (LLM)  ← 瘦身：只判 clarity + clarify_questions
  → ScenarioPlanResult {scenarios[], clarity, clarify_questions?}
  ↓
  ├─ clarity < 0.6 → ask_user 澄清 → 回填参数 → 重进 scenario_plan
  └─ clarity >= 0.6 → PlanGraph.expand(scenarios, user_msg, history)
                      ↓
                      tools[] + permissions + needs_approval + summary
                      ↓
                      (needs_approval? → propose_plan 卡片 → 等用户确认)
                      ↓
                    execute_plan ↔ evaluate_completion (最多 3 轮)
```

### 5.3 PlanGraph 输入 / 输出

**输入**：
```python
plangraph_input = {
    "scenarios": intent.scenarios,       # list[ScenarioMatch]
    "user_message": user_content,        # 原始用户消息（抽参数用）
    "history": short_term_context,       # 滑动窗口（抽 paper_id/project_id 用）
    "clarify_answers": [...],            # 若经过澄清，用户回填的参数
}
```

**输出**（合并后的 `ScenarioPlanResult` 兼容结构，供下游 `_execute_with_evaluation` 使用）：
```python
plangraph_output = {
    "scenario_id": "+".join(s.scenario_id for s in scenarios),  # "S1+S12"
    "summary": PlanGraph._render_summary(scenarios),  # 按路由表模板拼
    "needs_clarification": False,                     # 走到 PlanGraph 说明已澄清
    "clarification_questions": [],
    "needs_approval": max(danger_levels) >= "medium", # 硬映射
    "permissions_required": union(路由表 permissions),
    "estimated_time_seconds": sum(路由表 estimated),
    "tools": merged_tools,  # 含跨场景 depends_on
}
```

### 5.4 参数抽取（PlanGraph 内部，确定性代码）

每个 scenario 有专属抽取器，签名统一：

```python
def _extract_params(sid: str, user_msg: str, history: list[dict]) -> dict:
    """从用户消息 + history 抽取该场景所需参数，缺省用默认值。"""
    if sid == "S1":
        return {
            "query": _extract_query(user_msg),           # 去掉"找论文"等触发词后的主题词
            "sources": ["arxiv", "semantic_scholar"],
            "year_from": 2022,
            "max_results": 20,
        }
    elif sid == "S2":
        return {
            "query": _extract_query(user_msg),
            "max_results": 50,                           # 综述默认更大
            "enable_verify": "严格" in user_msg or "校验" in user_msg,
        }
    elif sid == "S13":
        return {"url": _extract_video_url(user_msg)}    # regex 抖音/TikTok 链接/口令
    elif sid == "S9":
        return {
            "seed_title": _extract_paper_ref(user_msg, history),
            "max_depth": 2,
            "direction": "both",
        }
    # ... 其余场景
```

抽取失败的参数 → 填 `None` → 交给 `scenario_plan` 的 `clarity` 判定（LLM 看到必填参数为 None → clarity < 0.6 → 生成 clarify_questions）。

---

## 6. 落地建议（非本设计文档范围，仅记录）

1. **先做 P0**：`sub_agent_task` 加 `agent_type` 分支。这解锁 5 个 C 档场景，ROI 最高。
2. **再做 S3 `create_subscription`**：独立小工具，不依赖 P0。
3. **PlanGraph 作为 `main_agent.py` 的新模块**：`src/paper_search/agent/plangraph.py`，导出 `SCENARIO_ROUTING` 表 + `expand()` 函数。`_node_scenario_plan` 改为调 `PlanGraph.expand()` 替代原 LLM 生成 tools[]。
4. **schema 瘦身**：`ScenarioPlanResult` 删字段需同步改 `SCENARIO_PLAN_SYSTEM` prompt（删除"生成 tools[]"相关指令）。
5. **B 档 stub 清理**：`extract_knowledge` / `paper_export` / `batch_search` / `compare_methods` 四个 stub 补真实实现（可独立于 PlanGraph 推进）。
6. **测试**：每个 scenario 路由表项加单元测试（参数抽取 + tools 展开 + danger_level）；P0 修复后加 6 个 graph 的端到端集成测试。

---

## 附录 A：路由表常量定义（伪代码，供实现参考）

```python
# src/paper_search/agent/plangraph.py

from dataclasses import dataclass, field
from typing import Literal

@dataclass
class RouteEntry:
    scenario_id: str
    name: str
    kind: Literal["sub_agent", "tool", "ios_tool"]
    exec_name: str                    # sub_agent type 或 tool name
    params_fn: str                    # _extract_params_S1 等函数名
    defaults: dict                    # 默认参数
    danger_level: Literal["low", "medium", "high"]
    permissions: list[str]
    estimated_time_seconds: int
    tier: Literal["A", "B", "C"]
    status: str                       # 实现状态说明

SCENARIO_ROUTING: dict[str, RouteEntry] = {
    "S1": RouteEntry("S1", "文献调研", "tool", "search_papers",
        "_extract_S1", {"sources": ["arxiv","semantic_scholar"], "year_from": 2022, "max_results": 20},
        "low", ["search"], 30, "A", "✅"),
    "S2": RouteEntry("S2", "综述生成", "sub_agent", "ingest",
        "_extract_S2", {"max_results": 50, "enable_verify": False},
        "medium", ["search", "download"], 600, "A", "✅"),
    "S3": RouteEntry("S3", "前沿追踪", "tool", "create_subscription",
        "_extract_S3", {"sources": ["arxiv","semantic_scholar"]},
        "low", ["subscription", "notification"], 5, "C", "❌ 缺工具"),
    # ... S4~S17 同理
}
```

## 附录 B：现有 graph 入口签名速查

| graph | 文件 | 构造函数 | state 必填字段 |
|---|---|---|---|
| IngestAgent | `graphs/ingest_graph.py` | `IngestAgent(runner, on_progress)` | `project_id, user_query, sources, year_from, max_results` |
| RADQueryAgent | `graphs/rad_query_graph.py` | `RADQueryAgent(knowledge_base, on_progress)` | `question, project_id, top_k, use_fulltext` |
| ClusteringAgent | `graphs/clustering_graph.py` | `ClusteringAgent(db, chroma_store, llm, on_progress)` | `project_id, n_clusters` |
| CitationChaseAgent | `graphs/citation_chase_graph.py` | `CitationChaseAgent(db, llm, engine, runner, on_progress)` | `seed_title/seed_doi, project_id, max_depth, direction` |
| TranslationAgent | `graphs/translation_graph.py` | `TranslationAgent(db, llm, chroma_store, on_progress)` | `action, text/direction/project_id` |
| VideoAgent | `graphs/video_graph.py` | `VideoAgent(downloader, whisper_model, llm, db, videos_dir, on_progress)` | `project_id, user_query` |
| HistoryAgent | `graphs/history_graph.py` | `HistoryAgent(db, memory, llm, on_progress)` | `messages, agent_id, session_id`（非 17 场景，内部用） |

所有 graph 均需 `.compile()` → `.ainvoke(state)` 调用。`on_progress` 回调签名：`async def(stage, index, total, current, paper_total)`。
