#!/usr/bin/env bash
# ============================================================
# backup-db.sh — 备份所有数据到 ~/paper_agent_backups/
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="$HOME/paper_agent_backups/$TIMESTAMP"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "=============================================="
echo "  Paper Agent — 数据备份"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo " 备份目录: ${BACKUP_DIR}"
echo "=============================================="
echo ""

mkdir -p "$BACKUP_DIR"

# ── 1. PostgreSQL ──────────────────────────────────────
log "备份 PostgreSQL (paper_search) ..."
export PGPASSWORD="${POSTGRES_PASSWORD:-paper_secret}"

if pg_isready -U paper_admin -h localhost -p 5432 -d paper_search >/dev/null 2>&1; then
    pg_dump -U paper_admin -h localhost -p 5432 \
        -Fc --no-owner --no-acl \
        -f "$BACKUP_DIR/paper_search.pgdump" \
        paper_search
    DUMP_SIZE=$(du -sh "$BACKUP_DIR/paper_search.pgdump" | cut -f1)
    ok "PostgreSQL 备份完成 (${DUMP_SIZE})"
else
    warn "PostgreSQL 不可达，跳过"
fi

# ── 2. Papers 目录 ─────────────────────────────────────
if [ -d "$HOME/papers" ]; then
    log "备份 ~/papers/ ..."
    tar czf "$BACKUP_DIR/papers.tar.gz" \
        --exclude='*.tmp' \
        -C "$HOME" papers/
    TAR_SIZE=$(du -sh "$BACKUP_DIR/papers.tar.gz" | cut -f1)
    ok "papers/ 备份完成 (${TAR_SIZE})"
else
    warn "~/papers/ 不存在，跳过"
fi

# ── 3. .paper_search 目录 ──────────────────────────────
if [ -d "$HOME/.paper_search" ]; then
    log "备份 ~/.paper_search/ ..."
    tar czf "$BACKUP_DIR/dot_paper_search.tar.gz" \
        --exclude='*/logs/*' \
        -C "$HOME" .paper_search/
    TAR_SIZE=$(du -sh "$BACKUP_DIR/dot_paper_search.tar.gz" | cut -f1)
    ok ".paper_search/ 备份完成 (${TAR_SIZE})"
else
    warn "~/.paper_search/ 不存在，跳过"
fi

# ── 4. Redis RDB (可选) ────────────────────────────────
if [ -f /var/lib/redis/dump.rdb ]; then
    log "备份 Redis RDB ..."
    sudo cp /var/lib/redis/dump.rdb "$BACKUP_DIR/redis_dump.rdb" 2>/dev/null || warn "Redis RDB 备份失败（权限不足）"
    ok "Redis RDB 备份完成"
else
    warn "Redis dump.rdb 不存在，跳过"
fi

# ── 5. docker-compose.yml 引用 ─────────────────────────
if [ -f "$PROJECT_ROOT/docker-compose.yml" ]; then
    cp "$PROJECT_ROOT/docker-compose.yml" "$BACKUP_DIR/"
    ok "docker-compose.yml 已备份"
fi

# ── 6. .env 引用 ───────────────────────────────────────
if [ -f "$PROJECT_ROOT/.env" ]; then
    cp "$PROJECT_ROOT/.env" "$BACKUP_DIR/dot_env.txt"
    ok ".env 已备份"
fi

# ── 更新 latest 符号链接 ──────────────────────────────
ln -snf "$BACKUP_DIR" "$HOME/paper_agent_backups/latest"

echo ""
echo "=============================================="
echo -e "  ${GREEN}✓ 备份完成${NC}"
echo "  目录: ${BACKUP_DIR}"
echo "  大小: $(du -sh "$BACKUP_DIR" | cut -f1)"
echo "  链接: ~/paper_agent_backups/latest"
echo "=============================================="
