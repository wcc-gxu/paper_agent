---
name: paper-agent
description: 学术论文搜索与管理全流程 — 意图澄清 → 搜索 → 评估 → 下载 → 转换 → 索引 → 排名 → 综述 → 导出。触发词：搜论文、文献综述、paper search、literature review、下载论文、论文调研。
allowed-tools: [Read, Write, Edit, Glob, Grep, Bash, AskUserQuestion, WebFetch, WebSearch, mcp__paper-search__search_papers, mcp__paper-search__download_paper, mcp__paper-search__convert_paper, mcp__paper-search__index_paper, mcp__paper-search__evaluate_papers, mcp__paper-search__rank_papers, mcp__paper-search__generate_survey, mcp__paper-search__paper_export, mcp__paper-search__paper_status, mcp__paper-search__paper_clean, mcp__paper-search__batch_search, mcp__paper-search__citation_chase, mcp__paper-search__list_sources]
---

# Paper Agent — 学术论文搜索与管理全流程

完整的 8 阶段论文研究工作流。从用户意图出发，到最终生成文献综述报告和 BibTeX 导出。

## 触发条件

当用户表达以下意图时激活此 skill：

- 搜索学术论文（"搜论文"、"找论文"、"search papers"）
- 做文献综述（"写综述"、"literature review"、"survey"）
- 调研某个研究方向（"调研一下 X 领域"、"X 方向有哪些论文"）
- 下载/管理论文（"下载这篇论文"、"帮我整理论文"）
- 论文评估/排名（"评估相关性"、"期刊等级"）

## 核心原则

### 1. 理解意图优先，不要直接搜

用户的一句话通常不足以构造精准的搜索 query。必须先问澄清问题，再生成搜索计划。

### 2. 每个阶段完成后评估结果，再决定下一步

不要无脑推进所有阶段。搜索完之后先看结果数量和摘要，再决定下载哪些。下载完成后检查 PDF 是否成功，再转换。

### 3. 批量操作优先

MCP 工具支持 `--all` 批量模式（convert、index、evaluate、rank），对已入库的论文尽量批量处理，减少逐条调用。

### 4. 项目 ID 贯穿全流程

第一次 `search_papers` 会返回 `project_id`，后续所有操作都传入此 ID，保持数据关联。

---

## 可用工具速查

### 核心流水线 (4)

| MCP Tool | 功能 | 关键参数 |
|----------|------|---------|
| `search_papers` | 跨多源搜索 → 写入 SQLite | `keywords`, `sources`, `year_from`, `year_to`, `max_results`, `project_id` |
| `download_paper` | 下载单篇 PDF | `title`, `source`, `paper_id`, `project_id` |
| `convert_paper` | PDF → Markdown | `paper_id`, `pdf_path`, `project_id --all` |
| `index_paper` | Markdown → ChromaDB 双 Collection | `paper_id`, `project_id --all`, `index_type` |

### 辅助工具 (9)

| MCP Tool | 功能 | 关键参数 |
|----------|------|---------|
| `evaluate_papers` | LLM 批量相关性评分 (0-1) | `project_id`, `query`, `paper_ids`, `--all` |
| `rank_papers` | 期刊等级评定 (CCF/SCI → A+/A/B/C) | `project_id --all`, `paper_id` |
| `generate_survey` | 生成 AI 文献综述 Markdown | `project_id`, `output` |
| `paper_export` | 导出 BibTeX / JSON | `project_id`, `format` |
| `paper_status` | 查看项目/论文状态 | `project_id`, `paper_id`, `limit` |
| `paper_clean` | 清理 DB/索引 | `project_id`, `--all`, `--keep-pdfs` |
| `batch_search` | 从 JSON/CSV 批量搜索 | `file_path`, `download`, `default_sources` |
| `citation_chase` | 1 层引用追踪 | `paper_title`, `doi` |
| `list_sources` | 来源可用性检查 | (无) |

---

## 完整工作流

### Phase 1: 意图澄清与计划生成

**目标**：将用户的模糊需求转化为可执行的搜索计划。

**步骤**：

1. 分析用户意图，识别缺失信息
2. 用 `AskUserQuestion` 问 3-5 个澄清问题：
   - **研究领域/方向** — 具体的研究问题是什么？
   - **关键词** — 英文关键词（AND/OR 逻辑）？
   - **时间范围** — 近 3 年？近 5 年？不限制？
   - **来源偏好** — arXiv（CS/AI）、PubMed（生物医学）、IEEE（电子工程）、CNKI（中文）？
   - **数量预期** — 希望最终保留多少篇？
3. 调用 `list_sources` 确认哪些来源当前可用
4. 生成搜索计划（口头描述即可，无需写文件），包含：
   - 搜索 query 列表（主搜索 + 补充搜索）
   - 来源选择（根据可用性）
   - 年份范围
   - 预期结果数量

**决策点**：
- 如果用户需求非常明确（有具体关键词、年份、来源），可跳过澄清直接搜索
- 如果 `list_sources` 返回的目标来源不可用，建议替代来源

---

### Phase 2: 搜索

**目标**：执行搜索，获取论文元数据列表。

**步骤**：

1. 调用 `search_papers`，参数：
   ```json
   {
     "keywords": "adversarial attack AND image classification",
     "sources": "arxiv,semantic_scholar",
     "year_from": 2023,
     "year_to": 2026,
     "max_results": 20,
     "project_id": null
   }
   ```
   - 首次搜索不传 `project_id`，系统自动创建项目
   - `max_results` 建议 20-50，根据需求调整

2. 解析返回的 JSON：
   - `project_id` — 记录此 ID，后续全部使用
   - `total_found` — 搜索结果总数
   - `papers` — 论文列表（标题、作者、年份、摘要前 500 字符、DOI、来源等）
   - `errors` — 哪些来源搜索失败

3. 向用户展示结果摘要：
   - 总共找到多少篇
   - 按来源分布
   - 按年份分布
   - 列出 Top 5-10 篇的标题和年份

**决策点**：
- 结果太少（< 5）→ 放宽关键词、扩大年份范围、增加来源，重新搜索
- 结果太多（> 100）→ 加筛选条件（更精确关键词、更高影响力来源）
- 某个来源报错 → 用其他来源补充
- 发现新的关键词方向 → 追加一轮搜索（传入已有的 `project_id`）

**多轮搜索**：
如果用户需求覆盖多个子方向，进行多轮搜索，每轮传入相同的 `project_id`：
```
search_papers(keywords="query1", ...)  → project_id: "abc123"
search_papers(keywords="query2", ..., project_id="abc123")  # 追加到同一项目
search_papers(keywords="query3", ..., project_id="abc123")
```

---

### Phase 3: 评估与筛选

**目标**：用 LLM 评估每篇论文与用户意图的相关性，筛掉不相关的。

**步骤**：

1. 调用 `evaluate_papers`：
   ```json
   {
     "project_id": "abc123",
     "query": "用户原始研究意图的完整描述",
     "all": true,
     "max_concurrent": 5
   }
   ```
   - `query` 应该是用户原始需求的完整描述，不只是关键词
   - `all: true` 评估项目下所有未评估的论文

2. 解析结果：
   - `relevant` / `irrelevant` — 相关/不相关计数
   - 每篇论文的 `score` (0-1) 和 `reason`

3. 向用户展示筛选结果，建议阈值：
   - `score >= 0.7` → 高相关，优先下载
   - `0.4 <= score < 0.7` → 中等相关，视数量决定
   - `score < 0.4` → 低相关，建议跳过

**决策点**：
- 高度相关论文太少（< 3）→ 放宽关键词重新搜索
- 评分普遍偏低 → 可能关键词不精准，重新搜索
- 评估完成后让用户确认下载列表

---

### Phase 4: 下载 PDF

**目标**：为筛选出的论文下载 PDF 全文。

**步骤**：

1. 对每篇确认下载的论文调用 `download_paper`：
   ```json
   {
     "title": "论文完整标题",
     "source": "arxiv",
     "paper_id": "arxiv:2401.xxxxx",
     "project_id": "abc123"
   }
   ```
   - **优先使用 `paper_id`**（从 Phase 2 搜索结果中的 `paper_ids` 获取）
   - `source` 必须与论文实际来源匹配

2. 下载结果：
   - `success: true` → PDF 已保存，`local_path` 为文件路径
   - `success: false` → 查看 `error` 信息，决定是否重试或跳过

**批量下载策略**：
- 每次最多并行 3-5 个下载（避免触发限流）
- arXiv PDF 通常直接可下
- 付费墙论文（ScienceDirect、IEEE）可能需要校园 IP
- 下载失败的论文记录原因，在最终报告中标注

**决策点**：
- 下载成功率 < 50% → 检查网络/IP，考虑只用 OA 来源
- 某篇论文无 PDF → 可跳过，摘要仍可用于综述

---

### Phase 5: PDF 转 Markdown

**目标**：将 PDF 转为结构化 Markdown，便于后续索引和阅读。

**步骤**：

1. **推荐批量转换**（项目下所有已下载 PDF）：
   ```json
   {
     "project_id": "abc123",
     "all": true,
     "max_concurrent": 2
   }
   ```
   - 自动跳过已转换的论文（检查 `markdown_path` 是否已存在）
   - `max_concurrent` 默认 2，避免内存峰值

2. 或单篇转换：
   ```json
   {
     "paper_id": "arxiv:2401.xxxxx"
   }
   ```

3. 结果检查：
   - `converted` — 成功转换数
   - `failed` — 失败数
   - 每篇的 `elapsed_seconds`

**注意**：
- 依赖 `pymupdf4llm`，需要 `pip install pymupdf4llm`
- 转换后的 .md 保存在 `~/papers/markdown/{project_id}/`
- 扫描版 PDF 效果差，文字型 PDF 效果好

---

### Phase 6: 索引入库

**目标**：将 Markdown 全文分块写入 ChromaDB，支持后续语义检索。

**步骤**：

1. **推荐批量索引**：
   ```json
   {
     "project_id": "abc123",
     "all": true,
     "index_type": "both"
   }
   ```
   - `index_type: "both"` — 同时索引摘要和全文
   - `index_type: "abstract"` — 仅摘要（快速筛选）
   - `index_type: "fulltext"` — 仅全文（深度检索）

2. 结果检查：
   - `indexed` — 成功索引数
   - 每篇的 `abstract_indexed` / `fulltext_chunks`

**ChromaDB 双 Collection 结构**：
- `papers_abstract` — 快速相关性筛选
- `papers_fulltext` — 深度内容检索

---

### Phase 7: 期刊等级评定

**目标**：标注每篇论文的发表期刊/会议等级。

**步骤**：

```json
{
  "project_id": "abc123",
  "all": true
}
```

**等级体系**：
- A+ — CCF-A / SCI-Q1 / 顶会顶刊
- A — CCF-B / SCI-Q2
- B — CCF-C / SCI-Q3
- C — SCI-Q4 / 其他

**结果**：
- 返回等级分布统计
- 无 venue 信息的论文标记为 `level: null`

---

### Phase 8: 综述生成与导出

**目标**：生成文献综述报告并导出引用。

**步骤**：

1. **生成综述**：
   ```json
   {
     "project_id": "abc123"
   }
   ```
   - 自动使用 `relevance_score >= 0.5` 的论文
   - 最多包含 50 篇
   - 输出到 `~/papers/outputs/{project_id}/survey.md`
   - 包含：搜索概况、研究方向分类、关键论文分析、未来方向建议

2. **导出 BibTeX**：
   ```json
   {
     "project_id": "abc123",
     "format": "bibtex"
   }
   ```
   - 输出到 `~/papers/outputs/{project_id}/references.bib`

3. **向用户展示最终成果**：
   - 综述报告摘要
   - 论文等级分布
   - 导出文件路径

---

## 迭代与判断逻辑

### 何时追加搜索

```
第一轮搜索 → 评估 → 如果「高相关论文 < 目标数量」
  → 分析原因：
    - 关键词不够精准 → 调整关键词重新搜索
    - 来源覆盖不足 → 增加来源（如加上 PubMed、ScienceDirect）
    - 年份范围太窄 → 放宽年份
  → 用同一 project_id 追加搜索
  → 仅对新论文执行 evaluate → download → convert → index
```

### 何时跳过某阶段

- **skip download**：用户只需要了解领域概况（搜 + 评估即可）
- **skip convert**：PDF 不需要全文检索（但无法 index 全文）
- **skip rank**：论文主要来自 arXiv（预印本无期刊信息）
- **skip survey**：用户只需要论文列表和元数据
- **skip export**：用户不需要 LaTeX/BibTeX 引用

### 处理错误

- **来源不可用** → 用 `list_sources` 确认，替换可用来源
- **PDF 下载失败** → 标记，跳过，继续处理其他论文
- **转换失败** → 检查 PDF 是否损坏、是否为扫描版
- **LLM 调用失败** → 降级为手动筛选（展示标题+摘要让用户判断）

---

## 输出文件结构

```
~/papers/
├── outputs/{project_id}/
│   ├── survey.md           # AI 文献综述报告
│   ├── references.bib      # BibTeX 导出
│   └── metadata.json       # 论文元数据（含等级）
├── markdown/{project_id}/
│   └── *.md                # 每篇论文的结构化全文
└── papers/
    └── {source}/{year}/
        └── {author}_{year}_{title}.pdf   # 下载的 PDF

~/.paper_search/
├── agent.db                # SQLite（项目、论文、关联、日志）
└── chroma/                 # ChromaDB 向量索引
    ├── papers_abstract/    # 摘要索引
    └── papers_fulltext/    # 全文分块索引
```

---

## 快速参考：完整流程示例

```
用户: "帮我调研近3年自动驾驶安全方向的论文"

1. 澄清 → 确认关键词: "autonomous vehicle safety AND (adversarial attack OR robustness)"
2. list_sources → 确认 arxiv, semantic_scholar 可用
3. search_papers(keywords="...", sources="arxiv,semantic_scholar", year_from=2023, max_results=30)
   → project_id: "proj_xxx"
4. evaluate_papers(project_id="proj_xxx", query="自动驾驶安全...", all=true)
   → 30 篇中 18 篇 relevant
5. 对 18 篇逐一下载 → 16 篇成功
6. convert_paper(project_id="proj_xxx", all=true)
7. index_paper(project_id="proj_xxx", all=true, index_type="both")
8. rank_papers(project_id="proj_xxx", all=true)
9. generate_survey(project_id="proj_xxx")
10. paper_export(project_id="proj_xxx", format="bibtex")
11. 向用户展示综述报告 + BibTeX 文件路径
```

## 搜索来源选择指南

| 研究领域 | 推荐来源 |
|---------|---------|
| CS / AI / ML | arxiv, semantic_scholar |
| 生物医学 | pubmed, semantic_scholar |
| 电子工程 | ieee, arxiv |
| 综合科学 | sciencedirect, semantic_scholar |
| 中文学术 | cnki |
| 跨学科 | semantic_scholar |

> **注意**：IEEE 和 ScienceDirect 需要校园 IP + API Key。CNKI 需要校园 IP 且可能有 CAPTCHA。如不可用，用 semantic_scholar 替代。
