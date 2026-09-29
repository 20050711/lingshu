#!/usr/bin/env bash
# tardis_ai 部署形态停止：只停 tardis 后端。
# **不 stop nginx**——nginx 进程可能同时承载同机其他站点，
# 停掉会顺带断掉别的入口。要停本入口请单独卸载站点：
#   sudo rm /etc/nginx/sites-enabled/tardis && sudo nginx -s reload
# 用法: bash deploy/stop.sh
set -euo pipefail
PIDFILE=/data/tardis/logs/tardis-backend.pid

if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  kill "$PID" 2>/dev/null || true
  for i in $(seq 1 15); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$PID" 2>/dev/null || true
  rm -f "$PIDFILE"
fi

echo "[OK] tardis 后端已停止（nginx/DB/Redis 及其他项目不受影响）"
