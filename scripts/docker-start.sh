#!/usr/bin/env bash
# ============================================================
# docker-start.sh — 一键 pull 镜像并启动所有 Docker 服务
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

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$HOME/paper_agent_backups/latest"

# ── 参数解析 ──────────────────────────────────────────
# 默认: PG 直接启动/重启，不做任何备份/还原
# --restore: 从 ~/paper_agent_backups/latest 还原（灾备用，会覆盖当前数据）
RESTORE_MODE="none"
for arg in "$@"; do
    case "$arg" in
        --restore) RESTORE_MODE="force" ;;
        *) err "未知参数: $arg（可用: --restore）"; exit 1 ;;
    esac
done

echo ""
echo "=============================================="
echo "  Paper Agent — Docker 启动"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
echo ""

# ── 前置检查 ──────────────────────────────────────────
if ! sudo docker compose version >/dev/null 2>&1; then
    err "docker compose 不可用，请先安装: sudo apt-get install docker-compose-v2"
    exit 1
fi

if ! sudo docker info >/dev/null 2>&1; then
    err "Docker daemon 未运行"
    exit 1
fi

cd "$PROJECT_ROOT"

# ── Step 1: Pull 镜像 ──────────────────────────────────
log "Step 1/6: 拉取 Docker 镜像 ..."
sudo docker compose pull 2>&1 | tail -5
ok "镜像拉取完成"

# ── Step 2: 停止本地服务 ──────────────────────────────
log "Step 2/6: 停止本地服务以释放端口 ..."

# 停止 bare-metal 应用进程
if [ -f scripts/start-all.sh ]; then
    bash scripts/start-all.sh --stop 2>/dev/null || true
fi

# 停止 Redis
if systemctl is-active --quiet redis-server 2>/dev/null; then
    log "  停止 redis-server ..."
    sudo systemctl stop redis-server
fi

# 停止 PostgreSQL
if systemctl is-active --quiet postgresql@16-main 2>/dev/null; then
    log "  停止 postgresql@16-main ..."
    sudo systemctl stop postgresql@16-main
elif systemctl is-active --quiet postgresql 2>/dev/null; then
    log "  停止 postgresql ..."
    sudo systemctl stop postgresql
fi

# ── Step 3: 等待端口释放 ──────────────────────────────
log "Step 3/6: 等待端口释放 ..."
for i in $(seq 1 15); do
    p1=$(ss -tln 2>/dev/null | grep -c ':5432 ' || true)
    p2=$(ss -tln 2>/dev/null | grep -c ':6379 ' || true)
    if [ "$p1" -eq 0 ] && [ "$p2" -eq 0 ]; then
        ok "端口 5432/6379 已释放"
        break
    fi
    sleep 1
done

# ── Step 4: 启动 Docker Compose ────────────────────────
log "Step 4/6: 启动 Docker Compose ..."
sudo docker compose up -d 2>&1
ok "容器已启动"

# ── Step 5: 等待 PostgreSQL Healthy ───────────────────
log "Step 5/6: 等待 PostgreSQL 就绪 ..."
STATUS=""
for i in $(seq 1 30); do
    STATUS=$(sudo docker compose ps postgres --format json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health',''))" 2>/dev/null || echo "")
    if [ "$STATUS" = "healthy" ]; then
        ok "PostgreSQL healthy"
        break
    fi
    sleep 2
done
if [ "$STATUS" != "healthy" ]; then
    err "PostgreSQL 未就绪，中止。请检查: sudo docker compose logs postgres"
    exit 1
fi

# ── Step 6: 数据还原（仅 --restore 时执行，默认直接启动）─
if [ "$RESTORE_MODE" != "force" ]; then
    log "Step 6/6: 跳过数据还原（PG 直接启动/重启；灾备还原用 --restore）"
else
    log "Step 6/6: --restore 从备份还原数据 ..."

    if [ ! -f "$BACKUP_DIR/paper_search.pgdump" ] && [ ! -f "$BACKUP_DIR/paper_search.sql" ]; then
        err "  未找到备份文件 ($BACKUP_DIR/paper_search.pgdump)"
        exit 1
    fi

    # 停掉应用容器，避免还原时持有连接/表锁
    log "  暂停应用容器 (api/worker/beat/daemon) ..."
    sudo docker compose stop api worker beat daemon >/dev/null 2>&1

    RESTORE_OK=1
    if [ -f "$BACKUP_DIR/paper_search.pgdump" ]; then
        log "  还原 PostgreSQL 数据 ..."
        if sudo docker compose exec -T postgres \
            pg_restore -U paper_admin -d paper_search \
            --clean --if-exists --no-owner --no-acl \
            < "$BACKUP_DIR/paper_search.pgdump" 2>&1 | tail -3; then
            ok "  PostgreSQL 数据还原完成"
        else
            RESTORE_OK=0
        fi
    elif [ -f "$BACKUP_DIR/paper_search.sql" ]; then
        log "  从 SQL 还原 ..."
        if sudo docker compose exec -T postgres \
            psql -U paper_admin -d paper_search \
            < "$BACKUP_DIR/paper_search.sql"; then
            ok "  PostgreSQL SQL 还原完成"
        else
            RESTORE_OK=0
        fi
    fi

    sudo docker compose start api worker beat daemon >/dev/null 2>&1
    if [ "$RESTORE_OK" -eq 0 ]; then
        err "  PostgreSQL 还原失败（应用容器已重启），请手动检查"
        exit 1
    fi

    # 还原 papers 卷（compose 卷实际名为 <project>_papers）
    PAPERS_VOLUME="$(basename "$PROJECT_ROOT" | tr '[:upper:]' '[:lower:]')_papers"
    if [ -f "$BACKUP_DIR/papers.tar.gz" ]; then
        log "  还原 papers 卷 ($PAPERS_VOLUME) ..."
        sudo docker run --rm -v "$PAPERS_VOLUME":/target -i busybox sh -c '
            rm -rf /target/* /target/.[!.]* /target/..?* 2>/dev/null
            tar xzf - -C /target || exit 1
            # 备份包内是 papers/ 顶层目录，摊平一层
            if [ -d /target/papers ]; then
                mv /target/papers/* /target/ 2>/dev/null || true
                mv /target/papers/.[!.]* /target/ 2>/dev/null || true
                rmdir /target/papers 2>/dev/null || true
            fi' < "$BACKUP_DIR/papers.tar.gz"
        ok "  papers 卷还原完成"
    fi
fi

# ── 等待全部服务启动 ──────────────────────────────────
echo ""
log "等待 API 服务就绪 ..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/api/health >/dev/null 2>&1; then
        ok "API 服务就绪"
        break
    fi
    sleep 2
done

# ── 显示状态 ──────────────────────────────────────────
echo ""
echo "=============================================="
echo -e "  ${GREEN}✓ Docker 启动完成${NC}"
echo "=============================================="
echo ""
echo "--- 容器状态 ---"
sudo docker compose ps
echo ""
echo "--- 地址 ---"
echo "  API:      http://localhost:8000"
echo "  API Docs: http://localhost:8000/paper/docs"
echo ""
echo "--- 常用命令 ---"
echo "  进入容器:   bash scripts/docker-enter.sh"
echo "  查看日志:   sudo docker compose logs -f api"
echo "  全部日志:   sudo docker compose logs -f"
echo "  停止:       sudo docker compose down"
echo "  强制还原:   bash scripts/docker-start.sh --restore  (灾备用，覆盖当前数据)"
echo "  回滚本地:   sudo docker compose down && sudo systemctl start postgresql redis-server"
echo "=============================================="
