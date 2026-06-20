#!/usr/bin/env bash
# Paper Agent v3 — 全服务启动脚本
#
# 启动/检查 5 个服务:
#   1. Redis Server
#   2. Celery Worker
#   3. Celery Beat (订阅定时检查)
#   4. API Server (uvicorn)
#   5. Agent Daemon
#
# 用法:
#   bash scripts/start-all.sh              # 启动全部
#   bash scripts/start-all.sh --status     # 仅检查状态
#   bash scripts/start-all.sh --stop       # 停止全部
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# ── 配置 ──────────────────────────────────────────────
REDIS_PORT="${REDIS_PORT:-6379}"
API_PORT="${API_PORT:-8000}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-4}"
# Auto-detect Python (prefer anaconda, then python3)
if [ -x "$HOME/anaconda3/bin/python" ]; then
    PYTHON="$HOME/anaconda3/bin/python"
elif command -v python3 &>/dev/null; then
    PYTHON="$(command -v python3)"
else
    PYTHON="python3"
fi
PID_DIR="/tmp/paper_agent_pids"

GREEN='\033[92m'; RED='\033[91m'; YELLOW='\033[93m'
CYAN='\033[96m'; RESET='\033[0m'; BOLD='\033[1m'

ok()   { echo -e "  ${GREEN}[OK]${RESET}   $1"; }
warn() { echo -e "  ${YELLOW}[WARN]${RESET}  $1"; }
err()  { echo -e "  ${RED}[ERROR]${RESET} $1"; }
info() { echo -e "  ${CYAN}[INFO]${RESET}  $1"; }

mkdir -p "$PID_DIR"

# ══════════════════════════════════════════════════════════
# Service Check Helpers
# ══════════════════════════════════════════════════════════

redis_running() {
    redis-cli -p "${REDIS_PORT}" ping &>/dev/null
}

celery_worker_running() {
    pgrep -f "celery.*paper_search.*worker" &>/dev/null
}

celery_beat_running() {
    pgrep -f "celery.*paper_search.*beat" &>/dev/null
}

# Wait for a process to be confirmed running
_wait_for() {
    local name="$1" check_fn="$2" max_wait="${3:-10}"
    local waited=0
    while [ $waited -lt $max_wait ]; do
        if $check_fn; then return 0; fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

api_running() {
    curl -s "http://localhost:${API_PORT}/api/health" &>/dev/null
}

daemon_running() {
    pgrep -f "paper_search.agent.daemon" &>/dev/null
}

# ══════════════════════════════════════════════════════════
# Status
# ══════════════════════════════════════════════════════════

show_status() {
    echo ""
    echo -e "${BOLD}Paper Agent v3 — Service Status${RESET}"
    echo "================================"
    redis_running     && ok "Redis (port $REDIS_PORT)"     || err "Redis — NOT RUNNING"
    celery_worker_running && ok "Celery Worker"            || err "Celery Worker — NOT RUNNING"
    celery_beat_running    && ok "Celery Beat"             || warn "Celery Beat — NOT RUNNING"
    api_running        && ok "API Server (port $API_PORT)" || err "API Server — NOT RUNNING"
    daemon_running     && ok "Agent Daemon"                || err "Agent Daemon — NOT RUNNING"
    echo ""
}

# ══════════════════════════════════════════════════════════
# Start Services
# ══════════════════════════════════════════════════════════

start_redis() {
    echo -e "${BOLD}── Redis${RESET}"
    if redis_running; then
        ok "Already running"
        return 0
    fi
    info "Starting Redis..."
    redis-server --daemonize yes --port "$REDIS_PORT" 2>&1 | head -1
    sleep 1
    if redis_running; then ok "Started (port $REDIS_PORT)"; else err "Failed to start"; return 1; fi
}

start_celery_worker() {
    echo -e "${BOLD}── Celery Worker${RESET}"
    if celery_worker_running; then
        ok "Already running"
        return 0
    fi
    if ! redis_running; then
        err "Redis not running — cannot start Celery Worker"
        return 1
    fi
    info "Starting Celery Worker (concurrency=$CELERY_CONCURRENCY)..."
    PYTHONPATH=src nohup "$PYTHON" -m celery \
        -A paper_search.agent.celery_app worker \
        --loglevel=info --concurrency="$CELERY_CONCURRENCY" \
        &> /tmp/paper_agent_celery_worker.log &
    echo $! > "$PID_DIR/celery_worker.pid"
    if _wait_for "Celery Worker" celery_worker_running 8; then
        ok "Started (PID $(cat "$PID_DIR/celery_worker.pid"))"
        info "Log: /tmp/paper_agent_celery_worker.log"
    else
        err "Failed to start — check /tmp/paper_agent_celery_worker.log"
        return 1
    fi
}

start_celery_beat() {
    echo -e "${BOLD}── Celery Beat (订阅定时)${RESET}"
    if celery_beat_running; then
        ok "Already running"
        return 0
    fi
    if ! redis_running; then
        err "Redis not running — cannot start Celery Beat"
        return 1
    fi
    info "Starting Celery Beat..."
    PYTHONPATH=src nohup "$PYTHON" -m celery \
        -A paper_search.agent.celery_app beat \
        --loglevel=info \
        &> /tmp/paper_agent_celery_beat.log &
    echo $! > "$PID_DIR/celery_beat.pid"
    if _wait_for "Celery Beat" celery_beat_running 6; then
        ok "Started (PID $(cat "$PID_DIR/celery_beat.pid"))"
        info "Log: /tmp/paper_agent_celery_beat.log"
    else
        err "Failed to start — check /tmp/paper_agent_celery_beat.log"
        return 1
    fi
}

start_api() {
    echo -e "${BOLD}── API Server${RESET}"
    if api_running; then
        ok "Already running (port $API_PORT)"
        return 0
    fi
    info "Starting API Server (uvicorn on 0.0.0.0:$API_PORT)..."
    PYTHONPATH=src nohup "$PYTHON" -m uvicorn \
        paper_search.api.app:app \
        --host 0.0.0.0 --port "$API_PORT" \
        &> /tmp/paper_agent_api.log &
    echo $! > "$PID_DIR/api.pid"
    sleep 3
    if api_running; then
        ok "Started (PID $(cat "$PID_DIR/api.pid"))"
        info "Log: /tmp/paper_agent_api.log"
        info "URL: http://localhost:${API_PORT}"
        info "WS:  ws://localhost:${API_PORT}/ws/chat/agent-001/main"
    else
        err "Failed to start — check /tmp/paper_agent_api.log"
        return 1
    fi
}

start_daemon() {
    echo -e "${BOLD}── Agent Daemon${RESET}"
    if daemon_running; then
        ok "Already running"
        return 0
    fi
    if ! redis_running; then
        err "Redis not running — cannot start Agent Daemon"
        return 1
    fi
    info "Starting Agent Daemon..."
    PYTHONPATH=src nohup "$PYTHON" -m paper_search.agent.daemon \
        &> /tmp/paper_agent_daemon.log &
    echo $! > "$PID_DIR/daemon.pid"
    sleep 3
    if daemon_running; then
        ok "Started (PID $(cat "$PID_DIR/daemon.pid"))"
        info "Log: /tmp/paper_agent_daemon.log"
    else
        err "Failed to start — check /tmp/paper_agent_daemon.log"
        return 1
    fi
}

# ══════════════════════════════════════════════════════════
# Stop Services
# ══════════════════════════════════════════════════════════

stop_all() {
    echo -e "${BOLD}Stopping all services...${RESET}"
    for name in daemon celery_beat celery_worker api; do
        pid_file="$PID_DIR/${name}.pid"
        if [ -f "$pid_file" ]; then
            pid=$(cat "$pid_file")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null && ok "Stopped $name (PID $pid)" || warn "Failed to stop $name"
            fi
            rm -f "$pid_file"
        fi
    done
    # Extra: kill any remaining processes by pattern
    pkill -f "paper_search.agent.daemon" 2>/dev/null || true
    pkill -f "celery.*worker.*paper_search" 2>/dev/null || true
    pkill -f "celery.*beat.*paper_search" 2>/dev/null || true
    pkill -f "uvicorn paper_search.api.app" 2>/dev/null || true
    echo ""
    echo -e "${GREEN}All services stopped.${RESET}"
}

# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════

case "${1:-}" in
    --status)
        show_status
        ;;
    --stop)
        stop_all
        ;;
    --help|-h)
        echo "Usage: bash scripts/start-all.sh [--status|--stop|--help]"
        echo ""
        echo "  (no args)   Start all 5 services (skip if already running)"
        echo "  --status    Show service status only"
        echo "  --stop      Stop all services"
        echo "  --help      This help"
        ;;
    *)
        echo ""
        echo -e "${BOLD}Paper Agent v3 — Start All Services${RESET}"
        echo "================================"
        echo ""

        redis_running && warn "Redis already running — skipping" || start_redis || true
        echo ""
        celery_worker_running && warn "Celery Worker already running — skipping" || start_celery_worker || true
        echo ""
        celery_beat_running && warn "Celery Beat already running — skipping" || start_celery_beat || true
        echo ""
        api_running && warn "API Server already running — skipping" || start_api || true
        echo ""
        daemon_running && warn "Agent Daemon already running — skipping" || start_daemon || true

        echo ""
        echo -e "${BOLD}────────────────────────────────${RESET}"
        show_status
        echo -e "${GREEN}Startup complete.${RESET}"
        ;;
esac
