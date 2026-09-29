#!/usr/bin/env bash
# 灵枢 · 部署形态启动（独立端口 / 独立日志 / 独立数据库，可与同机其他实例并存）
# 用法: bash deploy/start.sh
# 端口约定：后端 :8001 / nginx :24426
set -euo pipefail

ROOT=/opt/tardis
BACKEND_DIR=$ROOT/backend
LOG_DIR=/data/tardis/logs
PIDFILE=$LOG_DIR/tardis-backend.pid
BACKEND_LOG=$LOG_DIR/tardis-backend.log
PY=/opt/conda/envs/aip/bin/uvicorn
NGINX_SITE=/etc/nginx/sites-enabled/tardis

mkdir -p "$LOG_DIR"

# 0.5) 启动前滚动超限日志（>100MB 时改名为 .1）
if [ -f "$BACKEND_LOG" ] && [ "$(stat -c%s "$BACKEND_LOG" 2>/dev/null || echo 0)" -gt 104857600 ]; then
  mv "$BACKEND_LOG" "$BACKEND_LOG.1"
  echo "[i] tardis-backend.log 超 100MB 已滚动（.1）"
fi

# 0) 端口检查
if ss -ltn | grep -q ':24426 ' && ! pgrep -x nginx >/dev/null; then
  echo "[ERROR] :24426 被占用且 nginx 未运行，请先停止占用进程"
  exit 1
fi
if ss -ltn | grep -q ':8001 '; then
  echo "[ERROR] :8001 已有后端进程"
  echo "        请先停止旧后端: kill <pid>（或 bash deploy/stop.sh）"
  exit 1
fi

# 1) DB / Redis（systemd 服务，仅校验拉起；复用同机实例，库名/库号自行隔离）
pg_isready -q || sudo systemctl start postgresql
redis-cli -h 127.0.0.1 ping | grep -q PONG || sudo systemctl start redis-server

# 2) 后端：单 worker 硬约束（confirm 内存态 asyncio.Future + 队列降级进程内信号量，禁 --workers>1）
#    TRUSTED_PROXIES：配合 nginx 内部头恢复真实客户端 IP
#    --no-proxy-headers：SEC-07，关闭 uvicorn 层 XFF 消费，XFF 信任交给应用层 get_client_ip
export TRUSTED_PROXIES=127.0.0.1
export ENV_NAME=tardis
cd "$BACKEND_DIR"
nohup "$PY" app.main:app --host 0.0.0.0 --port 8001 --no-proxy-headers \
  >> "$BACKEND_LOG" 2>&1 &
echo $! > "$PIDFILE"

# 端口被残留进程占用时新 uvicorn 会立即退出——先确认本进程存活再等就绪
sleep 2
NEW_PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
if [ -z "$NEW_PID" ] || ! kill -0 "$NEW_PID" 2>/dev/null; then
  echo "[ERROR] 后端进程未存活（端口 8001 被占用？残留进程未清干净）。日志尾部："
  tail -5 "$BACKEND_LOG" 2>/dev/null
  echo "请先确认 8001 无残留进程（ps aux | grep uvicorn）再重试"
  exit 1
fi

# 等待就绪（最多 60s）
READY=0
for i in $(seq 1 60); do
  if curl -s -m 2 -o /dev/null http://127.0.0.1:8001/api/v1/health; then READY=1; break; fi
  sleep 1
done
if [ "$READY" != "1" ]; then echo "[ERROR] 后端 60s 未就绪，日志: $BACKEND_LOG"; exit 1; fi

# 2.5) 真机浏览器 MCP（real-browser/）：Streamable HTTP :18130，平台「MCP 工具」条目指向它
#      venv 不存在（未初始化）时跳过——不影响平台其余功能
BROWSER_MCP="$ROOT/real-browser"
if [ -x "$BROWSER_MCP/.venv/bin/python" ]; then
  if ss -ltn | grep -q ':18130 '; then
    echo "[i] 浏览器 MCP 已在运行（:18130）"
  else
    (cd "$BROWSER_MCP" && REAL_BROWSER_HOME=/data/tardis/real-browser \
      setsid nohup "$BROWSER_MCP/.venv/bin/python" -m agent_browser.mcp_server \
      --identity tardis --transport streamable-http --host 127.0.0.1 --port 18130 \
      > "$LOG_DIR/browser-mcp.log" 2>&1 < /dev/null &)
    sleep 3
    ss -ltn | grep -q ':18130 ' && echo "[i] 浏览器 MCP 已启动（:18130）" || echo "[WARN] 浏览器 MCP 启动失败，见 $LOG_DIR/browser-mcp.log"
  fi
else
  echo "[i] 跳过浏览器 MCP（real-browser/.venv 未初始化）"
fi

# 3) Nginx（只确保运行 + 本站点已装；绝不 stop）
if ! pgrep -x nginx >/dev/null; then
  sudo systemctl start nginx 2>/dev/null || sudo nginx
fi
if [ ! -e "$NGINX_SITE" ]; then
  echo "[WARN] nginx 站点未安装（$NGINX_SITE 不存在）——24426 入口不可用。安装："
  echo "       sudo cp $ROOT/deploy/tardis.nginx /etc/nginx/sites-available/tardis"
  echo "       sudo ln -s /etc/nginx/sites-available/tardis /etc/nginx/sites-enabled/tardis && sudo nginx -s reload"
fi

# 4) 全链路验证
sleep 1
echo "--- 验证 ---"
curl -s -o /dev/null -w '前端页面   :24426/             -> %{http_code}\n' http://127.0.0.1:24426/
curl -s -o /dev/null -w '健康检查   :24426/api/v1/health -> %{http_code}\n' http://127.0.0.1:24426/api/v1/health
curl -s -o /dev/null -w 'SPA 深链   :24426/admin        -> %{http_code}\n' http://127.0.0.1:24426/admin
if [ "${LLM_MOCK:-}" = "1" ]; then
  echo "LLM 档位   : **模拟档**（LLM_MOCK=1，测试零费用；走查看真实回复请不带该变量重启）"
else
  echo "LLM 档位   : **真实模型**（会调 DeepSeek 付费接口）"
fi
echo "后端 PID: $(cat "$PIDFILE")   日志: $BACKEND_LOG"
echo "[OK] tardis 部署形态已启动：http://127.0.0.1:24426（Windows 浏览器访问需 portproxy 24426，见部署说明）"
