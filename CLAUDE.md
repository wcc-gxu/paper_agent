# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

"智驭研" (ZhiYu Yan) is a Python research assistant server, part of a dual-agent system (iOS Agent + Python Agent). It serves AI security researchers and full-stack developers, compressing paper reading (3h → 1min) and video summarization (1h → 5min).

Current state: **Phase 1A complete** — paper search/download system operational. Application backend (FastAPI, Celery, vector stores) remains in the blueprint stage.

## Commands

```bash
# Install in dev mode
pip install -e ".[arxiv,pubmed,mcp,rich]"

# CLI — search papers
python -m paper_search.cli.main search "keywords" --sources arxiv,pubmed --max-results 10
python -m paper_search.cli.main search --title "Paper Title" --sources arxiv --format json
python -m paper_search.cli.main search --doi "10.1000/xyz" --sources semantic_scholar

# CLI — download PDF
python -m paper_search.cli.main download "Paper Title" --source arxiv --output ~/papers

# CLI — batch search from JSON/CSV file
python -m paper_search.cli.main batch queries.json --download

# CLI — list available sources and health
python -m paper_search.cli.main list-sources

# MCP Server (stdio transport for Claude Code)
python -m paper_search.mcp.server

# Existing utility scripts (standard library only)
python scripts/zotero_export.py --help       # Batch export PDFs from Zotero
python scripts/upload_all_pdf.py             # SCP upload with resume support
```

## Architecture

### Paper Search System (`src/paper_search/`)

**Provider plugin architecture** — each academic source implements `BaseProvider`:

```
CLI (argparse) ─┐
                ├── PaperSearchEngine ──┬── ArxivProvider (arxiv PyPI lib)
MCP (FastMCP)  ─┘   (fan-out + dedup)  ├── SemanticScholarProvider (REST API)
                                        ├── PubMedProvider (Bio.Entrez + metapub + Unpaywall)
                                        ├── CnkiProvider (stub, needs campus IP)
                                        ├── IeeeProvider (stub, needs campus IP)
                                        └── ScienceDirectProvider (stub, needs campus IP)
```

- **Providers** register via `@register(SourceType)` decorator — import the module to activate
- **Deduplication**: DOI exact match → title similarity ≥ 85%
- **PDF naming**: `{author}_{year}_{title}.pdf` — reuses `sanitize_path_segment()` and `_rename_duplicate()` patterns from `scripts/zotero_export.py`
- **Storage layout**: `{storage_dir}/{source}/{year}/{filename}.pdf`

### Key design decisions

- **Fan-out concurrency**: `asyncio.gather` across all requested sources, errors captured per-source (one failure doesn't block others)
- **Provider lifecycle**: lazy init + cached via `_get_provider()` in engine
- **MCP Server**: `FastMCP` v2 with `instructions=` kwarg (not `description`), `stdio` transport
- **Windows compatibility**: CLI wraps stdout with `io.TextIOWrapper(encoding='utf-8')` to avoid GBK emoji crashes
- **Free APIs need no auth**: arXiv (3s delay between requests), Semantic Scholar (100 req/min without key), PubMed (email required by NCBI, `metapub` for OA PDF resolution, Unpaywall fallback)

### Institutional access (Phase 1B, campus IP required)

- **CNKI**: Pure `requests` to `kns.cnki.net` search endpoints, CAPTCHA after ~1000 reqs → `pytesseract` OCR + manual fallback. Some papers only available as CAJ (skip and log).
- **IEEE/ScienceDirect**: Free metadata APIs for search, Playwright-based browser automation for PDF download
- **Cookie cache**: `~/.paper_search/cookies/{source}.json` to reduce browser launches
- Providers are stubs now; `health_check()` returns `False` when off-campus

## Existing Scripts (preserve these)

- `scripts/zotero_export.py` (865 lines) — reads Zotero SQLite DB, exports PDFs preserving collection hierarchy. **Reuse**: `sanitize_path_segment()`, `_rename_duplicate()`, `author_year_title` naming template, argparse CLI style.
- `scripts/upload_all_pdf.py` (129 lines) — resume-capable SCP upload to `124.156.201.202`, skips files with matching remote size. Rate-limited: 0.5s pause per 10 files.

## Dependencies (pyproject.toml)

- `fastmcp>=2,<3` — MCP server
- `httpx>=0.27` — async HTTP
- `pydantic>=2` — data models
- `arxiv>=2.3` — arXiv API client
- `biopython>=1.83`, `metapub>=0.6` — PubMed
- `playwright>=1.40` — browser automation (Phase 1B)
- `rich>=13` — CLI table output
- Python >= 3.11 (required for `asyncio.TaskGroup`)

## Blueprint Reference

The product design document at `docs/product/python-research-assistant.md` describes 4 planned modules (RAG Knowledge Base, Paper Analysis, Video Understanding, Daily Digest) with 30+ API endpoints. All are unimplemented. The document references Claude API, ChromaDB/Milvus, Celery+Redis, Faster-Whisper, and Docker Compose deployment — these are aspirational, not current.
