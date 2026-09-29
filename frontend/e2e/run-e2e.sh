#!/usr/bin/env bash
# 乱操作 E2E 一键编排（Playwright）
# 用法:
#   bash frontend/e2e/run-e2e.sh                     # 默认 deploy 入口全量
#   PROJECT=dev bash frontend/e2e/run-e2e.sh         # dev 入口
#   GREP="@bug1|@bug3" bash frontend/e2e/run-e2e.sh  # 按标签筛选
#   FUZZ_ROUNDS=3 FUZZ_STEPS=10 bash frontend/e2e/run-e2e.sh  # fuzz 规模
set -uo pipefail

ROOT=/opt/tardis
REPORT_DIR=$ROOT/e2e-report
PROJECT=${PROJECT:-deploy}
GREP=${GREP:-}
export FUZZ_ROUNDS=${FUZZ_ROUNDS:-10}
export FUZZ_STEPS=${FUZZ_STEPS:-20}
mkdir -p "$REPORT_DIR"

say() { echo "[e2e] $*"; }
fail() { say "预检失败: $*"; exit 1; }

# R9（2026-08-18）：Redis requirepass——所有 redis-cli 调用带密码（读 backend/.env，勿泄露）
REDIS_PW=$(grep -E '^REDIS_PASSWORD=' "$ROOT/backend/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)
RCLI=(redis-cli -a "$REDIS_PW" --no-auth-warning)

# ---- 预检 ----
say "预检开始（project=$PROJECT）"
curl -s -m 3 -o /dev/null http://127.0.0.1:8001/api/v1/health || fail "后端 :8001 未就绪（bash deploy/start.sh 或 dev 后端）"
curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:24426/ 2>/dev/null | grep -q 200 || fail "部署形态 :24426 未就绪（bash deploy/start.sh）"

if [ "$PROJECT" = "dev" ]; then
  DEV_UP=$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:5174/ 2>/dev/null || echo 000)
  if [ "$DEV_UP" != "200" ]; then
    say "dev :5174 未在线，自动拉起 npm run dev"
    (cd "$ROOT/frontend" && setsid nohup npm run dev > /tmp/vite-e2e.log 2>&1 < /dev/null &)
    for i in $(seq 1 30); do
      curl -s -m 2 -o /dev/null http://127.0.0.1:5174/ && { say "dev :5174 就绪"; DEV_STARTED=1; break; }
      sleep 1
    done
    curl -s -m 2 -o /dev/null http://127.0.0.1:5174/ || fail "dev :5174 拉起失败（日志 /tmp/vite-e2e.log）"
  fi
fi

"${RCLI[@]}" ping 2>/dev/null | grep -q PONG || fail "Redis 未就绪"

# 防爆破残留检查（正常登录绝不应有失败计数；2026-08-10 起键为 ip+username 双因子）
if "${RCLI[@]}" --scan --pattern 'auth:fail:*' 2>/dev/null | grep -q .; then
  say "警告: 存在防爆破计数残留——如测试登录出现验证码，执行: redis-cli del auth:fail:{ip}:{username}"
fi

# 模型档位断言（chaos 回落 model_layer.default，应为 flash+关思考）
# F7（2026-08-14）：source 输出混入断言——stdout 一并重定向（原仅 2>/dev/null，
# conda activate 的环境提示混进 FLASH 变量 → "实际=aip env: ..." 误报）
FLASH=$(cd "$ROOT/backend" && source scripts/env_aip.sh >/dev/null 2>&1 && python -c "
import asyncio
from app.core.database import get_global_engine
from sqlalchemy import text
async def main():
    e = get_global_engine()
    async with e.connect() as c:
        r = await c.execute(text(\"select value from system_config where key='model_layer.default'\"))
        row = r.first()
    await e.dispose()
    v = row[0] if row else '{}'
    print('flash' if 'flash' in str(v) else 'NO_FLASH')
asyncio.run(main())
" 2>/dev/null || echo 'CHECK_FAILED')
if [ "$FLASH" != "flash" ]; then
  say "警告: model_layer.default 未含 flash 档（实际=$FLASH）——LLM 档位与计划不符，但继续运行"
fi

# 上游夹具准备：>50MB 会议音频夹具按需生成（已存在则跳过，内容任意）。
# [ -f /tmp/upload_prog_60mb.wav ] || { python3 -c "open('/tmp/upload_prog_60mb.wav','wb').write(b'x'*(60*1024*1024))" && say "已生成夹具 /tmp/upload_prog_60mb.wav"; }

# 2026-09-17 踩坑 + 2026-09-18 再犯：MOCK=1 **只决定选哪些用例**，不会让服务端走模拟档——
# 服务端不是模拟档时，聊天类用例会真打付费模型（DeepSeek）。两次代价：09-17 那轮 49 次、
# 09-18 那轮 **128 次真实调用**。
# 原实现只 say 一句 + sleep 10 就继续——**后台跑时日志里根本看不见**，等于没拦。改为默认中止；
# 确需打真模型时显式 ALLOW_REAL_LLM=1（此时一般也不该再设 MOCK=1）。
if [ "${MOCK:-}" = "1" ]; then
  LLM_MOCK=$(curl -s -m 3 http://127.0.0.1:8001/api/v1/health 2>/dev/null | grep -o '"llm_mock":[a-z]*' | cut -d: -f2)
  if [ "$LLM_MOCK" != "true" ]; then
    if [ "${ALLOW_REAL_LLM:-}" = "1" ]; then
      say "⚠️  服务端非模拟档（llm_mock=${LLM_MOCK:-unknown}），已按 ALLOW_REAL_LLM=1 放行——会真调付费模型"
    else
      say "✗ MOCK=1 但服务端不是模拟档（llm_mock=${LLM_MOCK:-unknown}）——聊天类用例会真调 DeepSeek 付费模型，已中止。"
      say "  零费用跑法： (cd $ROOT/backend && LLM_MOCK=1 bash ../deploy/start.sh)  然后重跑本脚本"
      say "  确需打真模型：ALLOW_REAL_LLM=1 MOCK=1 bash run-e2e.sh"
      exit 1
    fi
  fi
fi

say "预检通过"

# ---- 运行 ----
cd "$ROOT/frontend/e2e"
ARGS=("--config=playwright.config.ts" "--project=$PROJECT")
[ -n "$GREP" ] && ARGS+=(--grep="$GREP")
# 2026-08-19：0 调用档/复杂组档分组——MOCK=1 排除 @real-llm（复杂任务等真 LLM 行为 spec，
# 由用户决定单独跑：GREP="@real-llm" bash run-e2e.sh）
if [ "${MOCK:-}" = "1" ]; then
  ARGS+=(--grep-invert="@real-llm")
fi
say "运行: npx playwright test ${ARGS[*]}"
START=$(date +%s)
npx playwright test "${ARGS[@]}"
CODE=$?
DURATION=$(( $(date +%s) - START ))

# ---- 汇总 ----
{
  echo ""
  echo "## Playwright 乱操作 E2E（$PROJECT）$(date '+%Y-%m-%d %H:%M')"
  echo "- 结果: $([ $CODE -eq 0 ] && echo 'PASS' || echo 'FAIL') | 耗时 ${DURATION}s | grep='$GREP' | fuzz=${FUZZ_ROUNDS}x${FUZZ_STEPS}"
} >> "$REPORT_DIR/SUMMARY.md"

if [ "$CODE" -eq 0 ]; then
  echo "=== PASS ==="
else
  echo "=== FAIL ==="
fi
echo "报告: $REPORT_DIR/html/index.html"
exit $CODE
