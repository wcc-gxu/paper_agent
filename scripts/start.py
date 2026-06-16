#!/usr/bin/env python3
"""Paper Agent v3 — 一键启动脚本。

流程:
  1. 环境检查 (Python / Redis / .env)
  2. 初始化 (DB / ChromaDB / Manifest)
  3. 启动 Celery Worker (后台子进程)
  4. 启动 FastAPI Server (uvicorn)
  5. 运行健康检测
  6. 打印访问地址

使用:
    python scripts/start.py                  # 默认配置
    python scripts/start.py --port 8080      # 指定端口
    python scripts/start.py --no-celery      # 不启动 Celery
    python scripts/start.py --skip-checks    # 跳过环境检查
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
os.chdir(str(PROJECT_ROOT))

# ── 颜色 ──────────────────────────────────────────────

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
CYAN = "\033[96m"; RESET = "\033[0m"; BOLD = "\033[1m"

def log(msg):     print(f"{CYAN}[start]{RESET} {msg}")
def ok(msg):      print(f"{GREEN}  [OK]{RESET} {msg}")
def warn(msg):    print(f"{YELLOW}  [WARN]{RESET} {msg}")
def err(msg):     print(f"{RED}  [ERROR]{RESET} {msg}")
def step(msg):    print(f"\n{BOLD}{msg}{RESET}")


# ── 子进程管理 ────────────────────────────────────────

_processes = []


def spawn(name: str, cmd: list[str]) -> subprocess.Popen:
    """启动后台子进程。"""
    log(f"Starting {name}: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    _processes.append((name, proc))
    time.sleep(1)
    if proc.poll() is not None:
        err(f"{name} failed to start (exit code {proc.returncode})")
        out = proc.stdout.read() if proc.stdout else ""
        if out: print(f"    {out[:500]}")
    else:
        ok(f"{name} started (PID {proc.pid})")
    return proc


def cleanup():
    """终止所有子进程。"""
    for name, proc in reversed(_processes):
        if proc.poll() is None:
            log(f"Stopping {name} (PID {proc.pid})...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _signal_handler(sig, frame):
    print(f"\n{YELLOW}Shutting down...{RESET}")
    cleanup()
    sys.exit(0)


# ── 检查 ──────────────────────────────────────────────

def check_python() -> bool:
    step("1. Checking Python")
    v = sys.version_info
    if v < (3, 11):
        err(f"Python {v.major}.{v.minor} — need >= 3.11")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    return True


def check_env() -> bool:
    step("2. Checking .env")
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        err(".env not found. Create from template with required API keys.")
        return False

    content = env_path.read_text()
    missing = []
    for key in ["VOLCANO_API_KEY", "SEMANTIC_SCHOLAR_API_KEY"]:
        if key not in content:
            missing.append(key)

    if missing:
        warn(f"Missing recommended keys: {', '.join(missing)}")
    ok(".env found")
    return True


def check_redis() -> bool:
    step("3. Checking Redis")
    try:
        import redis as _redis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        r = _redis.from_url(redis_url, socket_connect_timeout=3)
        r.ping()
        r.close()
        ok(f"Redis {redis_url} — OK")
        return True
    except Exception as e:
        err(f"Redis not available: {e}")
        warn("Celery worker will not start. Start Redis or use --no-celery.")
        return False


# ── 初始化 ────────────────────────────────────────────

def init_db():
    step("4. Initializing Database")
    from paper_search.agent.db import AgentDB
    db = AgentDB()
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    ok(f"SQLite ready — {len(tables)} tables")

    # Ensure default session exists
    existing = db.get_session("agent-001", "main")
    if not existing:
        db.create_session("agent-001", "main", title="新对话")
        ok("Default session created (agent-001/main)")

    db.close()


def init_manifest():
    step("5. Initializing Agent Manifest")
    manifest_path = Path.home() / ".paper_search" / "agent_manifest.json"
    if not manifest_path.exists():
        from paper_search.agent.daemon import AgentBootstrap
        import asyncio
        bs = AgentBootstrap()
        try:
            asyncio.run(bs.bootstrap())
            ok("Agent Manifest created (first boot)")
        except Exception as e:
            warn(f"Manifest creation deferred: {e}")
    else:
        ok("Agent Manifest exists — agent-001 ready")


def init_chromadb():
    step("6. Initializing ChromaDB")
    try:
        from paper_search.agent.chroma_store import ChromaStoreV2
        chroma = ChromaStoreV2()
        ok("ChromaDB ready")
    except Exception as e:
        warn(f"ChromaDB init deferred: {e}")


# ── 主流程 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Paper Agent v3 — One-click Start")
    parser.add_argument("--port", type=int, default=8000, help="API server port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="API server host (default: 0.0.0.0)")
    parser.add_argument("--no-celery", action="store_true", help="Skip Celery worker")
    parser.add_argument("--skip-checks", action="store_true", help="Skip environment checks")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload")
    args = parser.parse_args()

    print(f"\n{BOLD}Paper Agent v3 — One-click Start{RESET}\n")

    if not args.skip_checks:
        if not check_python(): sys.exit(1)
        if not check_env(): sys.exit(1)
        redis_ok = check_redis()
    else:
        redis_ok = False

    init_db()
    init_chromadb()
    init_manifest()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── Celery Worker ─────────────────────────────────
    if not args.no_celery and redis_ok:
        spawn("Celery Worker", [
            sys.executable, "-m", "celery",
            "-A", "paper_search.agent.celery_app",
            "worker", "--loglevel=info", "--concurrency=4",
            "--pool=threads",
        ])
    elif not args.no_celery:
        warn("Celery Worker skipped (Redis not available)")

    # ── FastAPI Server ────────────────────────────────
    step("7. Starting API Server")
    api_cmd = [
        sys.executable, "-m", "uvicorn",
        "paper_search.api.app:app",
        "--host", args.host,
        "--port", str(args.port),
    ]
    if args.reload:
        api_cmd.append("--reload")

    print()
    log(f"Starting API Server: uvicorn paper_search.api.app:app --host {args.host} --port {args.port}")
    print(f"\n{BOLD}{GREEN}  Paper Agent v3 is running!{RESET}")
    print(f"  API:      http://{args.host}:{args.port}")
    print(f"  Docs:     http://{args.host}:{args.port}/docs")
    print(f"  Health:   http://{args.host}:{args.port}/api/health")
    print(f"  WS:       ws://{args.host}:{args.port}/ws/chat/agent-001/main")
    print(f"\n  Press Ctrl+C to stop.\n")

    # 运行健康检测
    step("8. Health Check")
    try:
        subprocess.run([sys.executable, "scripts/health_check.py"], timeout=30)
    except subprocess.TimeoutExpired:
        warn("Health check timed out")
    except Exception as e:
        warn(f"Health check skipped: {e}")

    # 启动 API Server（前台，阻塞）
    try:
        proc = subprocess.run(api_cmd)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print(f"\n{GREEN}Paper Agent stopped. Goodbye!{RESET}")


if __name__ == "__main__":
    main()
