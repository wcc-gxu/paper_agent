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
DEPLOY_HOST=<server_ip> bash scripts/deploy.sh                 # 生产 (master)
DEPLOY_HOST=<server_ip> DEPLOY_ENV=test bash scripts/deploy.sh  # 测试 (dev)
```

## Git Flow + CI/CD

```
feature/* ──→ PR ──→ dev ──→ PR ──→ master
   │                │                │
   ▼                ▼                ▼
 CI: 跑测试      CI: 测试→构建    CI: 测试→构建
                 →推 :dev 镜像    →推 :latest 镜像
                 →测试机自动部署    →生产机手动部署
```

| 分支 | CI 行为 | 部署 |
|------|---------|------|
| `feature/*` | 跑测试 | — |
| `dev` | 测试 → 构建 → 推 `:dev` | 自动部署到测试机 |
| `master` | 测试 → 构建 → 推 `:latest` | 手动触发 (`workflow_dispatch`) |

**测试环境自动更新**：push 到 `dev` → CI 自动测试+构建+部署。**生产环境**：push 到 `master` 仅构建镜像，需在 Actions 页面手动触发 Deploy。

**GitHub Secrets**（Settings → Secrets → Actions）:

| Secret | 环境 | 说明 |
|--------|------|------|
| `TEST_SSH_HOST` / `PROD_SSH_HOST` | 服务器 IP |
| `TEST_SSH_USER` / `PROD_SSH_USER` | SSH 用户名 |
| `TEST_SSH_KEY` / `PROD_SSH_KEY` | SSH 私钥 |

**No lint, format, or typecheck config exists.**

## Security

- **`.env` is gitignored** — never commit API keys or secrets
- **Config defaults must use placeholders** — no real keys in `config.py` fallback values
- GitHub Push Protection enabled — commits with secrets will be rejected

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
