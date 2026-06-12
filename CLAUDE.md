# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start — 启动智能文献搜索 Agent

### 1. 一次性安装

```bash
pip install -e ".[arxiv,pubmed,mcp,rich]" pymupdf4llm chromadb
```

### 2. 配置 Claude Code 自动启动 MCP Server

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "paper-search": {
      "command": "python",
      "args": ["-m", "paper_search.mcp.server"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

Claude Code 启动后会自动拉起 MCP Server。如果不想自动启动，手动运行：

```bash
python -m paper_search.mcp.server
```

### 3. 确认服务正常

在 Claude Code 对话中说：**"列出可用文献来源"**

→ 会调用 `list_sources` tool，返回 6 个来源状态。

---

## 可用 MCP Tools（9 个）

### 全自动智能搜索（核心入口）

| Tool | 用途 | 示例 |
|------|------|------|
| `research` | **一句话触发全链路搜索** | "帮我搜 adversarial attack on LLM 近半年论文" |

### 底层原子操作

| Tool | 用途 |
|------|------|
| `search_papers` | 多源关键词搜索（arxiv/semantic_scholar/pubmed/sciencedirect） |
| `download_paper` | 下载单篇 PDF |
| `batch_search` | 从 JSON/CSV 文件批量搜索 |

### 管理与导出

| Tool | 用途 |
|------|------|
| `research_status` | 查看搜索项目进度 |
| `research_history` | 历史搜索项目列表 |
| `citation_chase` | 独立引用追踪（1层） |
| `export_report` | 导出 BibTeX / JSON 报告 |
| `list_sources` | 列出所有来源及可用状态 |

---

## `research` Tool 全链路流程

```
用户: "帮我搜 adversarial attack on large language models"
    │
    ├─ Stage 1:  LLM 意图解析 → 拆解为 3~5 个子查询 + 自动选来源 + 推断时间范围
    ├─ Stage 2:  策略规划 → 确定每轮搜索词和来源
    ├─ Stage 3:  迭代搜索 → 4源并发 → 去重 → LLM逐篇评估相关性(0~1分)
    │             → Agent自动判断是否继续搜（搜够了自动停）
    ├─ Stage 3.5: 期刊分级(CCF+SCI) + LLM提炼5个要点 + 方法标签 + 粗读/细读分级
    ├─ Stage 4:  PDF下载（全量相关论文）
    ├─ Stage 4.5: PDF→Markdown全文转换 (pymupdf4llm)
    ├─ Stage 5:  按章节分块 → ChromaDB向量入库
    ├─ Stage 6:  LLM自动聚类 → Wiki页面 + JSON元数据 + Survey综述
    ├─ Stage 7:  引用追踪 (可选)
    └─ Stage 8:  输出报告
```

### `research` 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `query` | string | 必填 | 自然语言搜索需求 |
| `enable_citation_chase` | bool | false | 是否启用引用追踪 |
| `max_iterations` | int | 3 | 最大搜索轮数 (1-5) |

### 全链路产出物

```
~/papers/outputs/{project_id}/
├── metadata.json          # 按 A+/A/B/C 等级分组的 JSON 元数据
├── wiki/
│   ├── index.md           # LLM 自动组织的文献综述首页
│   └── {研究方向}.md       # 各主题子页面（含结构化论文条目）
├── survey.md              # AI 生成的文献综述文章
├── report.md              # 搜索报告
└── references.bib         # BibTeX 引用

~/papers/markdown/{project_id}/
└── *.md                   # 每篇论文的全文 Markdown

~/.paper_search/
├── agent.db               # SQLite: 项目/论文/分级/标签
└── chroma/                # ChromaDB: 章节级向量索引
```

---

## 使用示例

在 Claude Code 对话中直接说：

```
"搜一下近半年 adversarial attack 在 LLM 安全方面的最新论文"
"帮我做一次全面的 LLM jailbreak 文献调研"
"找出所有关于 prompt injection defense 的论文，按期刊等级排序"
```

### CLI 快捷命令

```bash
# 查看来源状态
python -m paper_search.cli.main list-sources

# 快速搜索（不触发全链路Agent）
python -m paper_search.cli.main search "transformer" --sources arxiv,semantic_scholar --max-results 10

# 下载单篇
python -m paper_search.cli.main download "Attention Is All You Need" --source arxiv

# 现有 PDF 上传远端
python scripts/upload_all_pdf.py
```

---

## 环境配置 (.env)

项目根目录 `.env` 文件，已配置的 API Key：

| 变量 | 用途 | 状态 |
|------|------|------|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar 1req/s | ✅ |
| `ELSEVIER_API_KEY` | ScienceDirect 搜索+PDF | ✅ |
| `IEEE_API_KEY` | IEEE Xplore 搜索 | 🔑 等激活 |
| `VOLCANO_API_KEY` | 火山引擎 LLM (Agent大脑) | ✅ |

添加新 Key：直接编辑 `.env`，重启 MCP Server 即生效。

---

## 来源与能力矩阵

| 来源 | 搜索 | PDF | 需要 |
|------|------|-----|------|
| arXiv | ✅ | ✅ 直下 | 无 |
| Semantic Scholar | ✅ 1/s | ✅ OA | API Key (已有) |
| PubMed | ✅ | ✅ OA | 无 |
| ScienceDirect | ✅ 5k/周 | ✅ API | API Key (已有) + 校内IP |
| IEEE Xplore | 🔑 | 🌐 | API Key (等激活) + 校内IP |
| CNKI 知网 | ⚠️ | 🌐 | 校内IP + Playwright过验证码 |

---

## 依赖

```toml
# pyproject.toml
fastmcp>=2,<3       # MCP Server
httpx>=0.27          # HTTP
pydantic>=2          # 数据模型
arxiv>=2.3           # arXiv
biopython, metapub   # PubMed
pymupdf4llm          # PDF→Markdown
chromadb             # 向量库
python-dotenv        # 环境变量
rich>=13             # CLI输出
```

Python >= 3.11

---

## 项目结构

```
paper_agant/
├── src/paper_search/
│   ├── agent/              # 智能搜索 Agent
│   │   ├── agent.py        # 8-Stage 管道编排
│   │   ├── llm_client.py   # 火山引擎 LLM 调用
│   │   ├── db.py           # SQLite 持久化
│   │   ├── chroma_store.py # ChromaDB 向量索引
│   │   ├── pdf_converter.py # PDF→Markdown
│   │   ├── chunker.py      # 章节感知分块
│   │   ├── journal_ranker.py # CCF+SCI 分级
│   │   └── wiki_generator.py # Wiki+JSON生成
│   ├── providers/          # 6 个搜索来源 Provider
│   ├── downloaders/        # HTTP + Playwright 下载器
│   ├── mcp/server.py       # MCP Server (9 tools)
│   ├── cli/main.py         # CLI 入口
│   ├── engine.py           # 搜索引擎门面
│   ├── models.py           # Pydantic 数据模型
│   └── config.py           # 配置管理
├── scripts/                # 工具脚本 (Zotero导出/上传)
├── docs/product/           # 产品蓝图文档
├── .env                    # API Key 配置
└── pyproject.toml          # 项目元数据
```
