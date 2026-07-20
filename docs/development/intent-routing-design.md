# Paper Agent v5 — 意图-场景-节点路由设计

> v5.1 | 2026-07-20
>
> **核心设计**：意图即场景，场景即路由。一个意图映射到一个 handler 节点序列（单节点或依赖链）。不再有 v4 的独立“场景 ID”概念。

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **Intent = Scenario** | 意图就是业务场景。不再维护独立的 S1~S17 场景 ID 表，意图 ID 直接决定路由 |
| **确定性路由** | `intent_classify` 输出 primary intent → 路由表硬编码映射到 handler 节点序列 |
| **节点自治** | 每个 handler 内部处理工具调用、用户交互、错误。LLM 不参与执行编排 |
| **前置条件链** | 部分意图依赖论文已入库 → handler 检测前置条件 → 自动插入缺失链路节点 |
| **主/辅意图分离** | primary（1 个业务意图）+ side（0~N 个记忆写入意图），side 在 primary 前处理 |
| **用户交互统一** | 所有需要用户操作走 `ask` 消息；耗时任务进 Celery 前 `ask` 确认 |

---

## 二、意图路由总图

```
WS 消息 → BRPOP → Safety Filter → MainGraph.ainvoke()
                                         ↓
                   fast_triage ──→ chat ──→ inline_reply ──→ END
                        ↓
                   intent_classify ──→ ops ──→ ops_confirm → execute(ReAct) → END
                        ↓
                   side_handler（记忆写入）
                        ↓
                   [primary 路由表]
                        ↓
              handler 节点序列 → END
```

---

## 三、Primary Intent 路由表（14 业务意图）

| # | Primary Intent | Handler 节点序列 | 触发条件链 |
|---|---------------|-----------------|----------|
| 1 | `survey` | `literature_search_handler` → END | 无前置依赖 |
| 2 | `rag` | `rag_handler` → END | 无前置依赖 |
| 3 | `translation` | `translate_handler` → END | 无前置依赖 |
| 4 | `glossary` | `glossary_handler` → END | 无前置依赖 |
| 5 | `citation_chase` | `citation_handler` → END | 无前置依赖（S2 API） |
| 6 | `subscription` | `subscription_handler` → END | 无前置依赖（创建/查询订阅） |
| 7 | `video` | `video_handler` → END | 无前置依赖（handler 内多 tool 串行） |
| 8 | `memory` | `memory_handler` → END | 无前置依赖（查询记忆/偏好/语录） |
| 9 | `download` | `download_handler` → `convert_handler?` → `ingest_handler?` | 用户要求一步入库时自动链 |
| 10 | `paper_analysis` | `download→convert→ingest?` → `paper_handler` | 论文未入库时自动链 |
| 11 | `survey_generate` | `download→convert→ingest?` → `survey_handler` | 引用的论文未入库时自动链 |
| 12 | `writing` | `download→convert→ingest?` → `writing_handler` | 需分析的论文未入库时自动链 |
| 13 | `clustering` | `download→convert→ingest?` → `cluster_handler` | 待聚类论文未入库时自动链 |
| 14 | `ingest` | `ingest_handler` → END | PDF 已转为 MD 时直接入库 |

> `?` 表示节点内检测前置条件，不满足时自动触发上级节点再回到当前节点。不是每次都走。
>
> Intent #9~#13 中 `download→convert→ingest` 是**前置条件链路**，不是固有节点序列。

---

## 四、Side Intent（辅助意图，在 primary 前处理）

| Side Intent | 处理节点 | 工具 | 说明 |
|-------------|---------|------|------|
| `preference` | `side_handler` | `update_preference` | 用户偏好声明（“只看 CCF-A”“回答简短些”） |
| `feedback` | `side_handler` | `record_feedback` | 对回答的反馈（“上次太长了”“答案不准”） |
| `mentor_quote` | `side_handler` | `record_feedback` | 导师语录保存 |

side_handler 在 primary handler 前运行，因为偏好可能影响后续行为（如“只看 CCF-A” → search handler 自动加 venue filter）。side_handler 轻量 ReAct ≤ 3 轮。

---

## 五、技术意图（不列入业务场景）

| Intent | 节点 | 说明 |
|--------|------|------|
| `chat` | `inline_reply` | 非业务闲聊兜底，不计入科研场景 |
| `ops` | `ops_confirm` → `execute(ReAct)` | 运维操作（磁盘清理、服务管理、日志查看等） |

---

## 六、Handler 节点详细设计

### 6.1 `literature_search_handler` — 文献调研

**Trigger**: primary = `survey`

**工具序列**：`search_papers`(BM25+向量混合) → `evaluate_papers`(LLM 相关性评分)

**用户交互**：query 模糊 → `ask(text)` 先澄清；结果返回 → `message/reply(结果列表+摘要)` → turn 结束

**增量行为**：每次搜索记录到 `event_logs` 表；支持追加搜索（同一 session 内多轮）；搜索报告保存至文件

**实现状态**：✅

---

### 6.2 `rag_handler` — 知识库问答

**Trigger**: primary = `rag`

**工具序列**：`search_kb`(BM25+向量混合检索知识库) → LLM 综合回答

**反幻觉**：上下文为空或全部分数 < 阈值 → 拒答（“未查到相关信息”）；Chunk 附带 provenance metadata（paper_id, section, page）

**用户交互**：回答 → `ask(confirm)`（反馈收集：“有帮助吗？需要重新搜索吗？”） → turn 结束

**实现状态**：✅

---

### 6.3 `translate_handler` — 学术翻译

**Trigger**: primary = `translation`

**工具序列**：`glossary_search`(术语库检索) → LLM 翻译（术语替换+翻译）

**特殊逻辑**：先查用户词表，匹配的术语强制使用用户定义译法

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.4 `glossary_handler` — 词表管理

**Trigger**: primary = `glossary`

**工具序列**：`collect_terms`(TF-IDF 提取候选术语) → `verify_terms`(LLM 校验定义)

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.5 `citation_handler` — 引用追溯

**Trigger**: primary = `citation_chase`

**工具序列**：`resolve_paper` → `fetch_citations`(S2 API) → `filter_relevance`(LLM 过滤) → `summarize_citations`

**循环控制**：条件边控制追多层（`max_depth` 默认 2）

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.6 `subscription_handler` — 前沿追踪

**Trigger**: primary = `subscription`

**功能**：
- 创建订阅：`create_subscription(keywords, sources)` → 写 subscriptions 表
- 查看订阅：`list_subscriptions` / `get_subscription_results`
- 取消/修改订阅：`update_subscription` / `delete_subscription`

**后台执行**：Celery Beat 每 60 分钟触发 `subscription_check_task` → 命中新论文 → 走 APNs 推送

**Turn 结束**：✓

**实现状态**：🔧（`subscription_check_task` Beat 调度已存在，缺少主动触发入口和 create_subscription 工具）

---

### 6.7 `video_handler` — 视频解析

**Trigger**: primary = `video`

**工具序列（handler 内串行）**：
1. `download_video` — yt-dlp 下载（Celery，耗时）
2. `transcribe_video` — Whisper 转写（Celery，耗时）
3. `summarize_video` — LLM 结构化摘要
4. `save_capture` — 保存为碎片知识到 captures 表

**危险等级**：high（下载+转写占资源）

**运行时依赖**：yt-dlp, faster-whisper, ffmpeg

**Turn 结束**：✓

**实现状态**：🔧（graph 完整，dispatch 待接）

---

### 6.8 `memory_handler` — 记忆查询

**Trigger**: primary = `memory`

**工具序列**：`search_memory`(语义搜索) / `get_user_preference`(精确查询) / `list_memory`(列表查看)

**与 side_handler 关系**：side_handler 写入记忆（preference/feedback/mentor_quote），memory_handler 查询记忆

**Turn 结束**：✓

**实现状态**：🔧（工具已存在，handler 待实现）

---

### 6.9 `download_handler` — 论文下载

**Trigger**: primary = `download`

**工具序列**：
- N < 10 篇 → `download_paper`(s) inline 阻塞下载
- N ≥ 10 篇 → `ask(confirm, "后台运行？")` → Celery `download_papers_task`

**前置条件链**：用户要求下载后一步入库 → 自动链 `convert_handler` → `ingest_handler`

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.10 `paper_handler` — 论文精读

**Trigger**: primary = `paper_analysis`

**工具序列**：`search_kb(get_fulltext)` → LLM extract（创新点/方法/局限性结构化提取）

**前置条件链**：论文全文不在库中 → 自动链 `download→convert→ingest` → 再执行精读

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.11 `survey_handler` — 综述生成

**Trigger**: primary = `survey_generate`

**工具序列**：LLM generate survey（基于已入库论文）

**前置条件链**：引用的论文未入库 → 自动链 `download→convert→ingest` → 再生成综述

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.12 `writing_handler` — 写作辅助

**Trigger**: primary = `writing`

**工具序列（三个子任务，按需串行）**：
- `generate_survey` — 综述生成
- `check_ai_flavor` — AI 写作痕迹检测
- `gap_analysis` — 研究空白分析

**前置条件链**：需分析的论文未入库 → 自动链 `download→convert→ingest`

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.13 `cluster_handler` — 论文聚类

**Trigger**: primary = `clustering`

**工具序列**：`cluster_papers`(K-means) → LLM label → 可视化输出

**前置条件链**：待聚类论文未入库 → 自动链 `download→convert→ingest`

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.14 `ingest_handler` — 论文入库

**Trigger**: primary = `ingest`

**工具序列**：扫描指定目录 → `convert_to_md`(如未转换) → `chunk_embed_ingest`(切片+embedding+去重+入库)

**特殊逻辑**：默认扫描目录下全部 PDF，允许用户自定义路径

**Celery**：全程 Celery 异步

**Turn 结束**：✓

**实现状态**：✅

---

### 6.15 `convert_handler` — PDF 转 Markdown

**Trigger**: primary = `convert`

**工具序列**：`convert_to_md`（PyMuPDF4LLM）

**Celery**：是

**Turn 结束**：✓

**实现状态**：🔧

---

### 6.16 `inline_reply` — 闲聊兜底

**Trigger**: fast_triage → chat，或 intent_classify → chat

**行为**：纯 LLM 回复，不调工具

**Turn 结束**：✓

**实现状态**：✅

---

### 6.17 `ops_confirm → execute` — 运维操作

**Trigger**: primary = `ops`

**Ops 确认**：危险操作先确认（磁盘清理、服务重启等）

**Execute**：自由 ReAct 执行（仅 ops 路径保留 ReAct）

**包含子项**：
- `cleanup` — 磁盘清理：删除原始 PDF/MD 文件，保留 DB 记录（`os.walk` + `Path.unlink`）
- `service_start/stop/status` — 服务管理
- `bash_exec` — 命令执行（强制 approval + 审计）

**Turn 结束**：✓

**实现状态**：✅（cleanup ✅, service 工具 ✅, execute ✅）

---

## 七、前置条件链路详解

5 个意图在论文未入库时会**自动触发前置链路**，原理如下：

```
用户请求 paper_analysis/survey_generate/writing/clustering
       ↓
handler 检查：论文全文在知识库中？
       ├─ 在 → 直接执行
       └─ 不在 → 路由到 download_handler
                    ↓
                 下载完成 → 路由到 convert_handler
                    ↓
                 转换完成 → 路由到 ingest_handler
                    ↓
                 入库完成 → 路由回原 handler
```

**自动链条件判断**：每个 handler 入口检查 `state["has_md"]` / `state["has_ingested"]` 等标志位。缺失则返回路由信号给图引擎，由 `_route_to_handler` 转发到缺失节点。

**用户感知**：每次链切换推 `status` 消息（"正在下载..." → "正在转换..." → "正在入库..." → "正在生成综述..."），用户全程可见进度。

---

## 八、Tool 体系

| Tool | Handler 节点 | 来源 | 说明 |
|------|-------------|------|------|
| `search_papers` | literature_search | LiteratureAgent | BM25+向量混合搜索，跨源（arxiv/S2） |
| `evaluate_papers` | literature_search | LiteratureAgent | LLM 评估论文相关性 |
| `download_paper` | download | LiteratureAgent | 下载单篇 PDF |
| `convert_to_md` | convert / ingest | LiteratureAgent | PDF→MD 转换 |
| `search_kb` | rag / paper | KnowledgeAgent | BM25+向量检索知识库 |
| `chunk_embed_ingest` | ingest | KnowledgeAgent | 切片+embedding+去重+入库 |
| `generate_survey` | writing / survey | WritingAgent | LLM 生成文献综述 |
| `check_ai_flavor` | writing | WritingAgent | LLM 检测 AI 写作痕迹 |
| `gap_analysis` | writing | WritingAgent | LLM 分析研究空白 |
| `glossary_search` | translate | TranslationAgent | 词表检索匹配 |
| `collect_terms` | glossary | GlossaryAgent | TF-IDF 提取候选术语 |
| `verify_terms` | glossary | GlossaryAgent | LLM 校验术语定义 |
| `cluster_papers` | cluster | ClusteringAgent | K-means 聚类 |
| `fetch_citations` | citation | CitationChaseAgent | S2 API 获取引用 |
| `filter_relevance` | citation | CitationChaseAgent | LLM 过滤引用相关性 |
| `download_video` | video | VideoAgent | yt-dlp 下载视频（Celery） |
| `transcribe_video` | video | VideoAgent | Whisper 转写（Celery） |
| `summarize_video` | video | VideoAgent | LLM 结构化摘要 |
| `save_capture` | video | VideoAgent | 保存为碎片知识 |
| `create_subscription` | subscription | 新增 | 创建订阅 |
| `list_subscriptions` | subscription | 新增 | 查看订阅列表 |
| `search_memory` | memory | MemoryAgent | 语义搜索记忆 |
| `get_user_preference` | memory | MemoryAgent | 精确查询偏好 |
| `record_feedback` | side | 新增 | 记录反馈/偏好/语录 |
| `update_preference` | side | 已有 | 更新用户偏好 |

---

## 九、v4 场景 → v5 意图映射

| v4 Scenario | v5 Intent | 变化 |
|------------|----------|------|
| S1 文献调研 | `survey` | 1:1 |
| S2 综述生成 | `survey_generate` | 1:1 |
| S3 前沿追踪 | `subscription` | 升级为独立 intent（v4 缺失入口工具） |
| S4 论文精读 | `paper_analysis` | 1:1 |
| S5 方法对比 + S6 研究空白 | `writing` | 2→1（writing handler 内分子任务） |
| S7 进度查看 | **删除** | UI 层轮询能力，非 Agent 场景 |
| S8 聚类全景 | `clustering` | 1:1 |
| S9 引用追溯 | `citation_chase` | 1:1 |
| S10 RAG 问答 | `rag` | 1:1 |
| S11 批量搜索 | `survey` | 并入 survey handler |
| S12 翻译 | `translation` | 1:1 |
| S13 视频解析 | `video` | 升级为独立 intent（v4 dispatch bug 阻断） |
| S14 导出/清理 | `ops`(清理) / 导出去除 | 磁盘清理归入 ops；导出为 UI 能力非 Agent 场景 |
| S15 iOS 自动化 | **删除** | 客户端能力，非 Agent 场景 |
| S16 运维操作 | `ops` | 1:1 |
| S17 记忆操作 | `memory`(查询) + side_handler(写入) | 拆分为写入(side)和查询(primary)两个路径 |
| — | `download` | **新增**（v4 作为子步骤，v5 提升为独立意图） |
| — | `convert` | **新增**（同上） |
| — | `ingest` | **新增**（同上） |

---

## 十、与 plangraph-routing.md 的关系

`plangraph-routing.md`（17 场景 S1~S17 路由表）为 v4 设计稿，已被本文档取代。v5 不再维护独立的 S1~S17 场景 ID 表，意图 ID 即路由键。

---

> 相关文档:
> - [架构升级方案 v5](architecture-upgrade-v5.md)
> - [CLAUDE.md](../../CLAUDE.md) — 主架构参考
> - [plangraph-routing.md](plangraph-routing.md) — **[DEPRECATED]** v4 17 场景路由表
