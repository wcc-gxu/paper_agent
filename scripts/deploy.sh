#!/usr/bin/env bash
set -euo pipefail

# === 部署配置 ===
SSH_HOST="${DEPLOY_HOST:-}"
SSH_USER="${DEPLOY_USER:-root}"
SSH_PORT="${DEPLOY_PORT:-22}"
REMOTE_DIR="${DEPLOY_DIR:-/opt/paper_agant}"

# 环境: test / production（默认 production）
# test → 拉 :dev 镜像  production → 拉 :latest 镜像
DEPLOY_ENV="${DEPLOY_ENV:-production}"
if [ "$DEPLOY_ENV" = "test" ]; then
    IMAGE_TAG="dev"
    DEPLOY_DESC="测试环境"
else
    IMAGE_TAG="latest"
    DEPLOY_DESC="生产环境"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

die() { echo -e "${RED}✗ $1${NC}" >&2; exit 1; }
ok()  { echo -e "${GREEN}✓ $1${NC}"; }

if [ -z "$SSH_HOST" ]; then
    die "请设置 DEPLOY_HOST，例如: DEPLOY_HOST=192.168.1.100 bash scripts/deploy.sh"
fi

echo -e "${YELLOW}==> 部署到 ${DEPLOY_DESC}: ${SSH_USER}@${SSH_HOST}${NC}"

# 1. 检查 SSH
echo "==> 检查 SSH 连接..."
ssh -o ConnectTimeout=5 -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "echo ok" >/dev/null 2>&1 \
    || die "SSH 连接失败"
ok "SSH 正常"

# 2. 确保远端目录存在
ssh -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "mkdir -p ${REMOTE_DIR}"

# 3. 上传 docker-compose.yml
echo "==> 上传 docker-compose.yml..."
scp -P "$SSH_PORT" docker-compose.yml "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/"
ok "已上传"

# 4. 拉取镜像 + 重启
echo "==> 拉取镜像 (tag=${IMAGE_TAG}) 并重启..."
ssh -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" IMAGE_TAG="$IMAGE_TAG" REMOTE_DIR="$REMOTE_DIR" bash << 'DEPLOY'
set -e
cd ${REMOTE_DIR}

export IMAGE_TAG
docker compose pull api worker beat daemon 2>/dev/null || \
    docker-compose pull api worker beat daemon

docker compose up -d --remove-orphans 2>/dev/null || \
    docker-compose up -d --remove-orphans

echo ""
echo "=== 服务状态 ==="
docker compose ps 2>/dev/null || docker-compose ps
DEPLOY

ok "服务已更新"

# 5. 健康检查
echo "==> 健康检查..."
sleep 3
for i in 1 2 3 4 5; do
    if curl -sf "http://${SSH_HOST}:8000/api/health" >/dev/null 2>&1; then
        ok "健康检查通过 (${DEPLOY_DESC})"
        exit 0
    fi
    echo "  等待... (${i}/5)"
    sleep 2
done

die "健康检查失败，请检查: ssh ${SSH_USER}@${SSH_HOST} 'cd ${REMOTE_DIR} && docker compose logs api'"
