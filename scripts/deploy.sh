#!/usr/bin/env bash
set -euo pipefail

# === 远端服务器配置 ===
SSH_HOST="${DEPLOY_HOST:-}"          # 服务器 IP 或域名
SSH_USER="${DEPLOY_USER:-root}"      # SSH 用户名
SSH_PORT="${DEPLOY_PORT:-22}"        # SSH 端口
REMOTE_DIR="${DEPLOY_DIR:-/opt/paper_agant}"  # 部署目录

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

die() { echo -e "${RED}✗ $1${NC}" >&2; exit 1; }
ok()  { echo -e "${GREEN}✓ $1${NC}"; }

if [ -z "$SSH_HOST" ]; then
    die "请设置 DEPLOY_HOST 环境变量，例如: DEPLOY_HOST=192.168.1.100 ./scripts/deploy.sh"
fi

echo -e "${YELLOW}==> 部署到 ${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}${NC}"

# 1. 检查 SSH 连接
echo "==> 检查 SSH 连接..."
ssh -o ConnectTimeout=5 -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "echo ok" >/dev/null 2>&1 \
    || die "SSH 连接失败: ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
ok "SSH 连接正常"

# 2. 确保远端目录存在
ssh -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "mkdir -p ${REMOTE_DIR}"

# 3. 上传 docker-compose.yml
echo "==> 上传 docker-compose.yml..."
scp -P "$SSH_PORT" docker-compose.yml "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/"
ok "docker-compose.yml 已上传"

# 4. 拉取新镜像并重启
echo "==> 拉取镜像并重启服务..."
ssh -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" << DEPLOY
set -e
cd ${REMOTE_DIR}

# 拉取最新镜像
docker compose pull api worker beat daemon 2>/dev/null || \
    docker-compose pull api worker beat daemon

# 重启（仅更新有变更的服务）
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
    if curl -sf "http://${SSH_HOST}:8000/health" >/dev/null 2>&1; then
        ok "健康检查通过"
        exit 0
    fi
    echo "  等待... (${i}/5)"
    sleep 2
done

die "健康检查失败，请检查服务日志: ssh ${SSH_USER}@${SSH_HOST} 'cd ${REMOTE_DIR} && docker compose logs api'"
