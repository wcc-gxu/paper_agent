# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **工作流指导请使用 `/paper-agent` skill** — 包含从意图澄清到文献综述的完整 8 阶段流程。
> This file is a **reference card**; for operational workflows invoke the `paper-agent` skill.

## Quick Start

### 1. Install

```bash
pip install -e ".[arxiv,rich]" pymupdf4llm chromadb
```

### 2. MCP Server (auto-start via .mcp.json)

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

Verify: say **"列出可用文献来源"** → calls `list_sources` tool.

---

## MCP Tools (13)

All tools are registered via `.mcp.json` → `paper_search.mcp.server` → `@mcp.tool()` decorators.
Claude Code discovers them automatically as `mcp__paper-search__<name>`.

| MCP Tool | Function |
|----------|----------|
| `search_papers` | Multi-source search → dedup → SQLite |
| `download_paper` | Download single paper PDF |
| `convert_paper` | PDF → Markdown (pymupdf4llm) |
| `index_paper` | Markdown → ChromaDB dual-collection |
| `evaluate_papers` | LLM batch relevance scoring (0-1) |
| `rank_papers` | Journal ranking (CCF + SCI → A+/A/B/C) |
| `generate_survey` | Generate AI survey report |
| `paper_export` | Export BibTeX / JSON |
| `paper_status` | View project/paper state |
| `paper_clean` | Clean DB/index |
| `batch_search` | Batch search from JSON/CSV |
| `citation_chase` | 1-hop citation tracking |
| `list_sources` | Source health check |

---

## Data Flow

```
search_papers  →  (papers in SQLite)
    → evaluate_papers → filter by relevance_score
        → download_paper  →  (updates pdf_path)
            → convert_paper  →  (updates markdown_path)
                → index_paper  →  (ChromaDB: papers_abstract + papers_fulltext)

rank_papers      →  (unified_level: A+/A/B/C)
generate_survey  →  (survey.md report)
paper_export     →  (references.bib)

# All stages write progress to structured JSON logs:
#   ~/.paper_search/logs/agent.log  — global agent log
#   ~/.paper_search/logs/tasks/{task_id}.jsonl — per-task pipeline log
```

### Output Structure

```
~/papers/outputs/{project_id}/
├── survey.md               # AI survey report
├── references.bib          # BibTeX export
└── metadata.json           # Graded paper metadata

~/papers/markdown/{project_id}/
└── *.md                    # Full-text Markdown per paper

~/.paper_search/
├── agent.db                # SQLite: projects, papers, project_papers, journal_ranks
├── chroma/                 # ChromaDB: papers_abstract + papers_fulltext collections
└── logs/
    ├── agent.log           # Agent global log (JSONL, one event per line)
    └── tasks/              # Per-task pipeline logs
        └── task-{YYYYMMDD}-{seq}.jsonl
```

---

## Project Structure

```
paper_agant/
├── src/paper_search/
│   ├── agent/              # Core components
│   │   ├── db.py           # SQLite persistence (AgentDB)
│   │   ├── chroma_store.py # ChromaDB dual-collection
│   │   ├── pdf_converter.py # PDF→Markdown (pymupdf4llm)
│   │   ├── chunker.py      # Section-aware chunking
│   │   ├── journal_ranker.py # CCF+SCI journal ranking
│   │   ├── llm_client.py   # Volcano Engine LLM (intent/eval/report)
│   │   └── agent.py        # [DEPRECATED] Old 8-stage ResearchAgent
│   ├── cli/                # 10 CLI entry points
│   │   ├── common.py       # Rich console, DB/Engine factory, shared args
│   │   ├── search_cmd.py   # paper-search
│   │   ├── download_cmd.py # paper-download
│   │   ├── convert_cmd.py  # paper-convert
│   │   ├── index_cmd.py    # paper-index
│   │   ├── evaluate_cmd.py # paper-evaluate
│   │   ├── rank_cmd.py     # paper-rank
│   │   ├── survey_cmd.py   # paper-survey
│   │   ├── export_cmd.py   # paper-export
│   │   ├── status_cmd.py   # paper-status
│   │   └── clean_cmd.py    # paper-clean
│   ├── providers/          # 6 search source providers
│   ├── downloaders/        # HTTP + Playwright downloaders
│   ├── mcp/server.py       # MCP Server (13 tools, thin wrappers)
│   ├── engine.py           # PaperSearchEngine facade
│   ├── models.py           # Pydantic data models
│   └── config.py           # Configuration management
├── .claude/skills/         # Project skills
│   └── paper-agent/        # 完整论文研究工作流 skill
├── scripts/
├── .env                    # API keys
└── pyproject.toml
```

---

## Environment (.env)

| Variable | Purpose | Status |
|----------|---------|--------|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar 1 req/s | ✅ |
| `ELSEVIER_API_KEY` | ScienceDirect search + PDF | ✅ |
| `IEEE_API_KEY` | IEEE Xplore search | 🔑 pending activation |
| `VOLCANO_API_KEY` | Volcano Engine LLM | ✅ |

---

## Source Capability Matrix

| Source | Priority | Search | PDF | Requires |
|--------|----------|--------|-----|----------|
| Semantic Scholar | P0 | ✅ 1 req/s | ✅ OA | API Key |
| arXiv | P1 | ✅ | ✅ direct | None |
| PubMed | P1 | ✅ | ✅ OA | None |
| OpenAlex | P2 | ✅ 1 req/s | ❌ | None（无需 key） |
| ScienceDirect | P2 | ✅ 5k/week | ✅ API | API Key + campus IP |
| IEEE Xplore | P3 | 🔑 | 🌐 | API Key + campus IP |
| CNKI | P3 | ⚠️ | 🌐 | Campus IP + Playwright CAPTCHA |

> **搜索策略**：Semantic Scholar 作为元数据主来源（完整摘要 + AI 排序 + 引用关系 + OA PDF）→ arXiv/PubMed 并行补充 → 合并去重。
> PDF 下载顺序：Semantic Scholar OA → arXiv direct → ScienceDirect → IEEE → publisher page。
> 全部失败 → 记录到 `unavailable_pdfs` 表 → API 可查询。

---

## Dependencies

```
fastmcp>=2,<3       # MCP Server
httpx>=0.27          # HTTP
pydantic>=2          # Data models
arxiv>=2.3           # arXiv
biopython, metapub   # PubMed
pymupdf4llm          # PDF→Markdown
chromadb             # Vector DB
python-dotenv        # Environment
rich>=13             # CLI output
```

Python >= 3.11
