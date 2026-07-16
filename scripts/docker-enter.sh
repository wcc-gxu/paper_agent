#!/usr/bin/env bash
# ============================================================
# docker-enter.sh — 交互式进入 Docker 容器
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

# 切到项目根目录，保证任意位置执行都能找到 docker-compose.yml
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# ── 检查是否有运行中的容器 ────────────────────────────
if ! sudo docker compose ps --services 2>/dev/null | grep -q .; then
    echo "没有运行中的容器。请先执行: bash scripts/docker-start.sh"
    exit 1
fi

# ── 定义容器显示名和进入方式 ──────────────────────────
declare -A LABELS=(
    [postgres]="PostgreSQL 16 (pgvector)"
    [redis]="Redis 7"
    [api]="API Server (uvicorn, :8000)"
    [worker]="Celery Worker"
    [beat]="Celery Beat"
    [daemon]="Agent Daemon"
)

declare -A COMMANDS=(
    [postgres]="psql -U paper_admin -d paper_search"
    [redis]="redis-cli"
    [api]="/bin/bash"
    [worker]="/bin/bash"
    [beat]="/bin/bash"
    [daemon]="/bin/bash"
)

echo ""
echo "=============================================="
echo "  Paper Agent — 进入 Docker 容器"
echo "=============================================="
echo ""

# ── 列出容器 ──────────────────────────────────────────
i=1
declare -a ORDERED
while IFS= read -r svc; do
    LABEL="${LABELS[$svc]:-$svc}"
    STATUS=$(sudo docker compose ps --format json "$svc" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('State','?'))" 2>/dev/null || echo "?")
    echo -e "  ${CYAN}$i)${NC} $LABEL  ${GREEN}[$STATUS]${NC}"
    ORDERED+=("$svc")
    i=$((i+1))
done < <(sudo docker compose ps --services 2>/dev/null)

# 添加额外选项
echo -e "  ${CYAN}a)${NC} 查看全部日志 (docker compose logs -f)"
echo -e "  ${CYAN}q)${NC} 退出"
echo ""

read -p "选择 [1-$((i-1)) / a / q]: " CHOICE

case "$CHOICE" in
    q|Q)
        exit 0
        ;;
    a|A)
        sudo docker compose logs -f
        exit 0
        ;;
    *)
        if ! [[ "$CHOICE" =~ ^[0-9]+$ ]] || [ "$CHOICE" -lt 1 ] || [ "$CHOICE" -ge "$i" ]; then
            echo "无效选择"
            exit 1
        fi
        SVC="${ORDERED[$((CHOICE-1))]}"
        CMD="${COMMANDS[$SVC]:-/bin/bash}"
        echo ""
        echo -e "进入 ${GREEN}$SVC${NC} ..."
        echo ""
        sudo docker compose exec "$SVC" $CMD
        ;;
esac
