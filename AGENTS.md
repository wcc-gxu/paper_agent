# AGENTS.md — Paper Agent v4

## Dev commands

```bash
# Source is under src/ — PYTHONPATH=src is required for EVERYTHING
pip install -e ".[all]"

# Run all tests
PYTHONPATH=src pytest tests/ -v

# Run a single test file
PYTHONPATH=src pytest tests/test_main_agent_safety_intent.py -v

# Run a single test function
PYTHONPATH=src pytest tests/test_main_agent_safety_intent.py::TestSafetyRegex::test_normal_query_passes -v

# DB integration tests (skip by default unless this is set)
PYTEST_DB_INTEGRATION=1 PYTHONPATH=src pytest tests/test_ingest_agent.py -v

# Start all 5 services (Redis → Celery Worker → Celery Beat → API → Daemon)
bash scripts/start-all.sh
bash scripts/start-all.sh --status
bash scripts/start-all.sh --stop
```

## Docker

```bash
# 本地启动全部 6 个容器（postgres + redis + api + worker + beat + daemon）
docker compose up -d
docker compose ps
docker compose logs -f api

# 部署到远端服务器
DEPLOY_HOST=<server_ip> bash scripts/deploy.sh
```

## CI (GitHub Actions)

Push/PR 到 `master`/`main` 自动触发：测试 → 构建镜像 → 推送 `ghcr.io`。无需人工干预。

**No lint, format, or typecheck config exists.**

## Environment gotchas

- `.env` is **auto-loaded** by `config.py` at import — no manual sourcing needed
- `DATABASE_URL` is **mandatory** — PostgreSQL+pgvector only, no SQLite fallback
- Three Redis DB numbers: `0` (general), `1` (Celery broker), `2` (Celery results)
- Redis **must start first** before Celery Worker / Beat
- `test_ws_client.py` is a **standalone script**, not pytest — needs API running on port 8000
- `asyncio_mode = "auto"` in pyproject.toml — async tests auto-detected

## Architecture quick reference

```
src/paper_search/
├── agent/          # MainAgent + MainGraph + 8 sub-agent graphs + ToolRegistry
│   └── graphs/     # LangGraph StateGraphs (main_graph.py is the entry point)
├── api/            # FastAPI + WebSocket + outbox → WS/APNs dispatch
├── cli/            # 12 CLI entrypoints (paper-search, paper-download, …)
├── providers/      # 7 search sources (arxiv, semanticscholar, pubmed, …)
└── downloaders/    # HTTP + Playwright PDF downloaders
```

- **MainAgent** (daemon): BRPOP messages → safety filter → `MainGraph.ainvoke()`
- **MainGraph** (1790 lines): 10-node StateGraph (fast_triage → intent_classify → plan → gate → execute → evaluate)
- **Tool naming**: sub-agent tools use `agent_` prefix (e.g., `agent_literature_search`)
- **LangGraph thread_id = session_id** — enables cross-process resume via Checkpointer

## More details

See `CLAUDE.md` for full architecture docs, 17 scenarios, model strategies, and PostgreSQL schema.
