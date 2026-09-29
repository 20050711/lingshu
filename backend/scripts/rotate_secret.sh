#!/bin/bash
# R1（红队三修复）：每日 02:00 cron 入口——轮换 SECRET_KEY + 重启 uvicorn（无 reload 模式需重启生效）
# cron 注册：crontab -l 追加 `0 2 * * * cd /opt/tardis/backend && bash scripts/rotate_secret.sh >> /tmp/rotate_secret.log 2>&1`
set -e
cd "$(dirname "$0")/.."

# VSCode 会话 .venv 污染清理（HANDOVER 踩坑 11）
export PATH="/opt/conda/envs/aip/bin:$PATH"
source /opt/conda/conda.sh 2>/dev/null || true

python "$(dirname "$0")/rotate_secret.py"

# 重启 uvicorn（分两步：先 kill 后拉起；避免 pkill -f 自匹配陷阱——用 pgrep 精确匹配 uvicorn 进程）
PIDS=$(pgrep -f "uvicorn app.main:app" || true)
if [ -n "$PIDS" ]; then
  echo "[rotate_secret] kill uvicorn PIDs: $PIDS"
  kill $PIDS 2>/dev/null || true
  sleep 2
fi
setsid nohup /opt/conda/envs/aip/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn.log 2>&1 < /dev/null &
echo "[rotate_secret] uvicorn 已重启"
