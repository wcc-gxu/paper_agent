# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **架构文档索引**: [agent-runloop.md](docs/development/agent-runloop.md) — AgentRunLoop · [architecture.md](docs/development/architecture.md) — 系统拓扑 · [websocket-protocol.md](docs/development/websocket-protocol.md) — WS 协议

## Quick Start

### 1. Install

```bash
pip install -e ".[arxiv,rich]" pymupdf4llm chromadb
```

### 2. Tool Use (13 CLI + MCP 运行时)

MCP Server 作为运行时工具暴露层，底层 13 个工具均为 CLI 可独立调用。

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

## CLI Tools (13)

所有工具通过 `src/paper_search/cli/` 独立调用，同时也是 Agent Tool Use 的基础。

| CLI Tool | Function |
|----------|----------|
| `search_papers` | Multi-source search → dedup → SQLite |
| `download_paper` | Download single paper PDF |
| `convert_paper` | PDF → Markdown (pymupdf4llm) |
| `index_paper` | Markdown → ChromaDB 6-collection |
| `evaluate_papers` | LLM batch relevance scoring (0-1) |
| `rank_papers` | Journal ranking (CCF + SCI → A+/A/B/C) |
| `generate_survey` | Generate AI survey report |
| `paper_export` | Export BibTeX / JSON |
| `paper_status` | View project/paper state |
| `paper_clean` | Clean DB/index |
| `batch_search` | Batch search from JSON/CSV |
| `citation_chase` | 1-hop citation tracking |
| `list_sources` | Source health check |
| `web_search` | Volcengine web search (500次/月) |

---

## Data Flow

### 论文入库流水线 (IngestAgent)
```
search_papers  →  (papers in SQLite)
    → evaluate_papers → filter by relevance_score
        → download_paper  →  (updates pdf_path)
            → convert_paper  →  (updates markdown_path)
                → index_paper  →  (ChromaDB: 6 collections)

rank_papers      →  (unified_level: A+/A/B/C)
generate_survey  →  (survey.md report)
paper_export     →  (references.bib)
```

### 事件流 (AgentRunLoop)
```
iOS → WebSocket → RunLoop(prio=0) → PlanGraph
Tool调用 → Celery Worker → Redis BRPOP → RunLoop(prio=1~2) → PlanGraph → iOS
Timer  → RunLoop(prio=3) → PlanGraph → 工具执行
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
├── agent_manifest.json     # Agent 身份证 — 启动/恢复/迁移
├── agent.db                # SQLite: projects, papers, project_papers, journal_ranks
├── chroma/                 # ChromaDB: 6 collections (papers_abstract, papers_fulltext, agent_conversations, agent_knowledge, agent_expressions, agent_learnings)
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
| `WEB_SEARCH_API_KEY` | Volcano Engine Web Search (500次/月) | ✅ |

---

## Source Capability Matrix

| Source | Priority | Search | PDF | Requires |
|--------|----------|--------|-----|----------|
| Semantic Scholar | P0 | ✅ 1 req/s | ✅ OA | API Key |
| arXiv | P1 | ✅ | ✅ direct | None |
| PubMed | P1 | ✅ | ✅ OA | None |
| OpenAlex | P2 | ✅ 1 req/s | ❌ | None（无需 key，Semantic Scholar 降级方案） |
| ScienceDirect | P2 | ✅ 5k/week | ✅ API | API Key + campus IP |
| IEEE Xplore | P3 | 🔑 | 🌐 | API Key + campus IP |
| CNKI | P3 | ⚠️ | 🌐 | Campus IP + Playwright CAPTCHA |

> **搜索策略**：Semantic Scholar 作为元数据主来源（完整摘要 + AI 排序 + 引用关系 + OA PDF）→ arXiv/PubMed 并行补充 → OpenAlex 作为 Semantic Scholar 降级方案（免费无 Key）→ 合并去重。
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
