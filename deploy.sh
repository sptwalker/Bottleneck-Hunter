#!/usr/bin/env bash
# 一键部署：备份 → 拉取 main → 重建镜像 → 健康检查。反复可用。
# 用法（服务器项目根目录）：./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

HOST_PORT="${HOST_PORT:-8089}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.observability.yml"
APP=bottleneck-hunter

echo "==> [1/5] 备份数据快照（保留最近 5 份）"
cp -r data "data.bak.$(date +%Y%m%d_%H%M%S)"
ls -dt data.bak.* 2>/dev/null | tail -n +6 | xargs -r rm -rf

echo "==> [2/5] 拉取 main（fast-forward，不覆盖本地改动）"
git fetch origin main
git pull --ff-only origin main

echo "==> [3/5] 重建镜像并重启（HOST_PORT=$HOST_PORT）"
HOST_PORT="$HOST_PORT" $COMPOSE up -d --build

echo "==> [4/5] 清理悬空镜像"
docker image prune -f >/dev/null || true

echo "==> [5/5] 健康检查 /healthz"
ok=0
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}/healthz" || true)
  if [ "$code" = "200" ]; then ok=1; echo "  healthz OK (200)"; break; fi
  echo "  等待应用就绪... ($i) 当前=$code"; sleep 2
done
[ "$ok" = "1" ] || { echo "!! 健康检查未过，看日志: docker logs $APP --tail 80"; exit 1; }

echo "==> 部署完成: $(git rev-parse --short HEAD)"
$COMPOSE ps
