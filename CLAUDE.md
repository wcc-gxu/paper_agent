# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Architecture: Modular CLI + Claude Code Harness

The old 8-stage monolithic `research` pipeline is replaced by **10 independent CLI tools**.
Claude Code acts as the **harness**: understand intent → ask questions → generate plan → call CLIs step by step → aggregate → next round.

### Core Data Flow CLIs (4)

| CLI | Purpose | Example |
|-----|---------|---------|
| `paper-search` | Multi-source search → dedup → SQLite | `paper-search --keywords "adversarial attack" --sources arxiv --year-from 2024` |
| `paper-download` | Download single paper PDF | `paper-download --paper-id "arxiv:2401.xxx" --source arxiv` |
| `paper-convert` | PDF → Markdown (pymupdf4llm) | `paper-convert --paper-id "arxiv:2401.xxx"` |
| `paper-index` | MD → ChromaDB dual-collection (abstract + fulltext) | `paper-index --paper-id "arxiv:2401.xxx" --project-id <id>` |

### Auxiliary CLIs (6)

| CLI | Purpose |
|-----|---------|
| `paper-evaluate` | LLM batch relevance scoring (0-1) |
| `paper-rank` | Journal ranking (CCF + SCI → A+/A/B/C) |
| `paper-survey` | Generate AI survey report |
| `paper-export` | Export BibTeX / JSON |
| `paper-status` | View project/paper state |
| `paper-clean` | Clean DB/index (--keep-pdfs flag) |

### MCP Tools (v2, 8 tools)

| Tool | Maps to |
|------|---------|
| `search_papers` | paper-search (writes SQLite) |
| `download_paper` | paper-download |
| `batch_search` | Batch search from JSON/CSV |
| `list_sources` | Source health check |
| `paper_status` | paper-status |
| `paper_export` | paper-export |
| `paper_clean` | paper-clean |
| `citation_chase` | 1-hop citation tracking |

> **Removed:** `research` (old monolithic 8-stage pipeline)

---

## Claude Code Harness Workflow

```
User: "搜近3年自动驾驶安全新论文"
    │
    ├─ Claude Code 分析意图 → 生成 4+ 澄清问题 → 用户回答
    ├─ Claude Code 生成 plan.json + plan.md
    │
    ├─ paper-search --keywords "..." --sources ... --project-id <id>
    │   → stdout JSON → Claude Code 解析结果
    │
    ├─ Claude Code 评估搜索结果 → 决定下载列表
    │
    ├─ paper-download --paper-id "..." (per paper)
    ├─ paper-convert --paper-id "..."
    ├─ paper-index --paper-id "..." --project-id <id>
    │
    ├─ Claude Code 判断是否需要下一轮搜索
    │   → Yes: 生成新 plan → 重复
    │   → No: 进入汇总
    │
    └─ paper-survey --project-id <id>
       paper-export --project-id <id> --format bibtex
```

### CLI Design Principles

- **stdout**: JSON (machine-readable, pipeable)
- **stderr**: Rich formatted (progress bars, tables, colors for humans)
- **State**: SQLite `~/.paper_search/agent.db` for all intermediate state
- **ChromaDB**: Dual collections — `papers_abstract` (fast filter) + `papers_fulltext` (deep search)
- **Error handling**: Non-zero exit codes + stderr detail; caller decides retry
- **Each CLI is independently callable** from terminal, scripts, or Claude Code

---

## Data Flow

```
paper-search → (papers in SQLite)
    → Claude Code evaluates → selects which to download
        → paper-download → (updates pdf_path)
            → paper-convert → (updates markdown_path)
                → paper-index → (updates embedding_id, chunks in ChromaDB)
```

### Output Structure

```
~/papers/outputs/{project_id}/
├── plan.json / plan.md     # Claude Code generated plan
├── metadata.json           # Graded paper metadata (A+/A/B/C)
├── survey.md               # AI survey report
└── references.bib          # BibTeX export

~/papers/markdown/{project_id}/
└── *.md                    # Full-text Markdown per paper

~/.paper_search/
├── agent.db                # SQLite: projects, papers, project_papers, journal_ranks
└── chroma/                 # ChromaDB: papers_abstract + papers_fulltext collections
```

---

## Project Structure

```
paper_agant/
├── src/paper_search/
│   ├── agent/              # Core components
│   │   ├── db.py           # SQLite persistence (AgentDB)
│   │   ├── chroma_store.py # ChromaDB dual-collection (ChromaStore + ChromaStoreV2)
│   │   ├── pdf_converter.py # PDF→Markdown (pymupdf4llm)
│   │   ├── chunker.py      # Section-aware chunking
│   │   ├── journal_ranker.py # CCF+SCI journal ranking
│   │   ├── llm_client.py   # Volcano Engine LLM (intent/eval/report)
│   │   └── agent.py        # [DEPRECATED] Old 8-stage ResearchAgent pipeline
│   ├── cli/                # 10 independent CLI tools
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
│   │   ├── clean_cmd.py    # paper-clean
│   │   └── main.py         # Legacy CLI (backward compat)
│   ├── providers/          # 6 search source providers
│   ├── downloaders/        # HTTP + Playwright downloaders
│   ├── mcp/server.py       # MCP Server v2 (8 tools, thin wrappers)
│   ├── engine.py           # PaperSearchEngine facade
│   ├── models.py           # Pydantic data models
│   └── config.py           # Configuration management
├── scripts/
├── .env                    # API keys
└── pyproject.toml          # 11 console_scripts entry points
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

| Source | Search | PDF | Requires |
|--------|--------|-----|----------|
| arXiv | ✅ | ✅ direct | None |
| Semantic Scholar | ✅ 1 req/s | ✅ OA | API Key |
| PubMed | ✅ | ✅ OA | None |
| ScienceDirect | ✅ 5k/week | ✅ API | API Key + campus IP |
| IEEE Xplore | 🔑 | 🌐 | API Key + campus IP |
| CNKI | ⚠️ | 🌐 | Campus IP + Playwright CAPTCHA |

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
