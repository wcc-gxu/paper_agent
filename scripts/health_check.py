#!/usr/bin/env python3
"""Paper Agent 综合健康检测。

检查 10+ 个组件，输出彩色报告。

使用:
    python scripts/health_check.py
    python scripts/health_check.py --json   # JSON 输出
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    import redis as _redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

# ── 终端颜色 ────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

def ok(msg):   return f"{GREEN}[OK]{RESET} {msg}"
def fail(msg): return f"{RED}[FAIL]{RESET} {msg}"
def warn(msg): return f"{YELLOW}[WARN]{RESET} {msg}"
def info(msg): return f"{CYAN}[INFO]{RESET} {msg}"


# ═══════════════════════════════════════════════════════════════
# 检查项
# ═══════════════════════════════════════════════════════════════


class HealthChecker:
    def __init__(self):
        self.checks: list[dict] = []
        self.all_passed = True

    def _add(self, name: str, passed: bool, detail: str = "", warning: bool = False):
        self.checks.append({"name": name, "passed": passed, "detail": detail, "warning": warning})
        if not passed and not warning:
            self.all_passed = False

    def check_all(self) -> list[dict]:
        self._check_python()
        self._check_disk()
        self._check_env()
        self._check_db()
        self._check_chromadb()
        self._check_redis()
        self._check_llm_api()
        self._check_search_apis()
        self._check_manifest()
        self._check_logs()
        self._check_dependencies()
        return self.checks

    # ── 各项检查 ───────────────────────────────────────

    def _check_python(self):
        v = sys.version_info
        if v >= (3, 11):
            self._add("Python Version", True, f"{v.major}.{v.minor}.{v.micro}")
        else:
            self._add("Python Version", False, f"{v.major}.{v.minor}.{v.micro} (need >= 3.11)")

    def _check_disk(self):
        try:
            usage = shutil.disk_usage(str(Path.home()))
            free_gb = round(usage.free / (1024**3), 1)
            if free_gb < 1:
                self._add("Disk Space", False, f"{free_gb} GB free", warning=True)
            elif free_gb < 10:
                self._add("Disk Space", True, f"{free_gb} GB free (low)", warning=True)
            else:
                self._add("Disk Space", True, f"{free_gb} GB free")
        except Exception as e:
            self._add("Disk Space", False, str(e))

    def _check_env(self):
        env_path = Path(__file__).parent.parent / ".env"
        if not env_path.exists():
            self._add(".env File", False, "Not found — create from .env.example")
            return

        required = {
            "VOLCANO_API_KEY": "火山方舟 LLM",
            "SEMANTIC_SCHOLAR_API_KEY": "Semantic Scholar 搜索",
        }
        optional = {
            "WEB_SEARCH_API_KEY": "火山引擎联网搜索",
            "ELSEVIER_API_KEY": "ScienceDirect",
            "IEEE_API_KEY": "IEEE Xplore",
        }

        missing_required = []
        missing_optional = []
        content = env_path.read_text()

        for key, desc in required.items():
            if key not in content or not _extract_value(content, key):
                missing_required.append(f"{key} ({desc})")

        for key, desc in optional.items():
            if key not in content or not _extract_value(content, key):
                missing_optional.append(f"{key} ({desc})")

        if missing_required:
            self._add("API Keys (required)", False, f"Missing: {', '.join(missing_required)}")
        elif missing_optional:
            self._add("API Keys", True, f"Required OK. Optional missing: {', '.join(missing_optional)}", warning=True)
        else:
            self._add("API Keys", True, "All configured")

    def _check_db(self):
        from paper_search.agent.db import AgentDB
        try:
            db = AgentDB()
            tables = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [r[0] for r in tables]
            paper_count = db.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            project_count = db.conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            session_count = db.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            msg_count = db.conn.execute("SELECT COUNT(*) FROM ws_messages").fetchone()[0]
            db.close()
            self._add("SQLite (agent.db)", True,
                      f"{len(table_names)} tables, {paper_count} papers, {project_count} projects, "
                      f"{session_count} sessions, {msg_count} msgs")
        except Exception as e:
            self._add("SQLite (agent.db)", False, str(e))

    def _check_chromadb(self):
        from paper_search.agent.chroma_store import ChromaStoreV2
        try:
            chroma = ChromaStoreV2()
            # Try to get collection info
            try:
                client = chroma._client
                cols = client.list_collections()
                col_names = [c.name for c in cols]
                self._add("ChromaDB", True, f"{len(col_names)} collections: {', '.join(col_names[:6])}")
            except Exception:
                self._add("ChromaDB", True, "Connected (collections lazy-loaded)")
        except Exception as e:
            self._add("ChromaDB", False, str(e))

    def _check_redis(self):
        if not HAS_REDIS:
            self._add("Redis", False, "redis package not installed", warning=True)
            return

        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        try:
            r = _redis.from_url(redis_url, socket_connect_timeout=3)
            start = time.monotonic()
            r.ping()
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            r.close()
            self._add("Redis", True, f"{redis_url} — PING {elapsed_ms}ms")
        except _redis.ConnectionError:
            self._add("Redis", False, f"{redis_url} — Connection refused. Start redis-server.", warning=True)
        except Exception as e:
            self._add("Redis", False, str(e), warning=True)

    def _check_llm_api(self):
        api_key = os.getenv("VOLCANO_API_KEY", "")
        if not api_key:
            self._add("LLM API (Volcano)", False, "VOLCANO_API_KEY not set")
            return

        if not HAS_HTTPX:
            self._add("LLM API (Volcano)", False, "httpx not installed")
            return

        try:
            import asyncio
            async def _test():
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        "https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={"model": "deepseek-v4-pro", "messages": [{"role":"user","content":"hi"}], "max_tokens":1},
                    )
                    return resp.status_code
            status = asyncio.run(_test())
            if status in (200, 401, 403, 429):
                self._add("LLM API (Volcano)", True, f"Connected (HTTP {status})")
            else:
                self._add("LLM API (Volcano)", False, f"HTTP {status}")
        except Exception as e:
            self._add("LLM API (Volcano)", False, str(e)[:100])

    def _check_search_apis(self):
        # Semantic Scholar
        s2_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
        if s2_key and HAS_HTTPX:
            try:
                import asyncio
                async def _test_s2():
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1",
                            headers={"x-api-key": s2_key},
                        )
                        return resp.status_code
                status = asyncio.run(_test_s2())
                if status == 200:
                    self._add("Semantic Scholar API", True, "OK")
                else:
                    self._add("Semantic Scholar API", False, f"HTTP {status}")
            except Exception as e:
                self._add("Semantic Scholar API", False, str(e)[:100])
        else:
            self._add("Semantic Scholar API", False, "Key not configured", warning=True)

        # arXiv (no key needed)
        if HAS_HTTPX:
            try:
                import asyncio
                async def _test_arxiv():
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            "https://export.arxiv.org/api/query?search_query=all:test&max_results=1",
                        )
                        return resp.status_code
                status = asyncio.run(_test_arxiv())
                if status == 200:
                    self._add("arXiv API", True, "OK (no key)")
                else:
                    self._add("arXiv API", False, f"HTTP {status}")
            except Exception as e:
                self._add("arXiv API", False, str(e)[:100])

        # WEB_SEARCH_API_KEY
        web_key = os.getenv("WEB_SEARCH_API_KEY", "")
        if web_key:
            self._add("Web Search (Volcengine)", True, "Key configured")
        else:
            self._add("Web Search (Volcengine)", True, "Key not configured (web_search unavailable)", warning=True)

    def _check_manifest(self):
        manifest_path = Path.home() / ".paper_search" / "agent_manifest.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text())
                agent = data.get("agent", {})
                self._add("Agent Manifest", True,
                          f"agent={agent.get('agent_id','?')}, status={agent.get('status','?')}")
            except Exception:
                self._add("Agent Manifest", False, "Invalid JSON")
        else:
            self._add("Agent Manifest", True, "Not created yet (first boot will create)", warning=True)

    def _check_logs(self):
        log_dir = Path.home() / ".paper_search" / "logs"
        task_dir = log_dir / "tasks"
        if log_dir.exists():
            global_log = log_dir / "agent.log"
            global_size = global_log.stat().st_size if global_log.exists() else 0
            task_count = len(list(task_dir.glob("*.jsonl"))) if task_dir.exists() else 0
            self._add("Logs", True, f"agent.log ({_fmt_size(global_size)}), {task_count} task logs")
        else:
            self._add("Logs", False, "~/.paper_search/logs/ not found", warning=True)

    def _check_dependencies(self):
        deps = {
            "httpx": "HTTP client",
            "fastapi": "Web framework",
            "uvicorn": "ASGI server",
            "langgraph": "Agent framework",
            "celery": "Task queue",
            "redis": "Redis client",
            "pymupdf4llm": "PDF converter",
            "chromadb": "Vector DB",
            "pydantic": "Data validation",
        }
        missing = []
        for mod, desc in deps.items():
            try:
                __import__(mod.replace("-", "_"))
            except ImportError:
                missing.append(f"{mod} ({desc})")

        if missing:
            self._add("Dependencies", False, f"Missing: {len(missing)} packages", warning=True)
        else:
            self._add("Dependencies", True, f"{len(deps)} core packages OK")

    # ── 打印 ──────────────────────────────────────────

    def print_report(self):
        print(f"\n{BOLD}Paper Agent v3 — Health Check{RESET}\n")
        print(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  CWD:  {os.getcwd()}")
        print()

        passed = 0
        failed = 0
        warnings = 0

        for c in self.checks:
            status_icon = ok("") if c["passed"] else (warn("") if c.get("warning") else fail(""))
            status_text = "PASS" if c["passed"] else ("WARN" if c.get("warning") else "FAIL")
            print(f"  [{status_text}] {c['name']}")
            if c["detail"]:
                print(f"         {c['detail']}")

            if c["passed"] and not c.get("warning"):
                passed += 1
            elif c.get("warning"):
                warnings += 1
            else:
                failed += 1

        print(f"\n{BOLD}Summary:{RESET} {ok(str(passed))} {warn(str(warnings))} {fail(str(failed))}")
        if not self.all_passed:
            print(f"\n{YELLOW}Run 'python scripts/start.py' to initialize components.{RESET}")

    def to_json(self) -> str:
        return json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "checks": self.checks,
            "summary": {
                "passed": sum(1 for c in self.checks if c["passed"] and not c.get("warning")),
                "warnings": sum(1 for c in self.checks if c.get("warning")),
                "failed": sum(1 for c in self.checks if not c["passed"] and not c.get("warning")),
            },
        }, ensure_ascii=False, indent=2)


# ── Helpers ────────────────────────────────────────────

def _extract_value(content: str, key: str) -> str:
    """提取 .env 中 KEY=value 的 value。"""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith(f"{key}="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            return val if val and val != "your_key" else ""
    return ""


def _fmt_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes/1024:.1f} KB"
    else:
        return f"{size_bytes/(1024**2):.1f} MB"


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    checker = HealthChecker()
    checker.check_all()

    if "--json" in sys.argv:
        print(checker.to_json())
    else:
        checker.print_report()
