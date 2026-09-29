#!/bin/bash
# 测试 LLM 免费档切换（2026-08-17 用户决策：测试不用付费 LLM，优先免费模型）
# 切换**所有** model_layer.* 键的 employee 段（llm + llm_aux）→ agnes-2.5-flash（$0，RPM 20-30）
# （key 链 dept_id 优先于 default——只改 default 覆盖不到 model_layer.market 等团队档）
# 测完还原 deepseek 原值。走平台 /chat/ask 的测试脚本与前端 e2e 依赖此切换。
#
# 用法:
#   bash scripts/switch_test_model.sh on    # 切免费档（备份各键原值到 /tmp/model_layer.bak.json + 重启 uvicorn）
#   bash scripts/switch_test_model.sh off    # 还原 + 重启 uvicorn
set -e
cd "$(dirname "$0")/.."
source scripts/env_aip.sh

BAK=/tmp/model_layer.bak.json
ACTION=${1:?用法: bash scripts/switch_test_model.sh [on|off]}

python - <<PY
import asyncio, json, pathlib
from app.core.database import get_global_engine
from sqlalchemy import text

FREE = {"platform": "agnes", "model": "agnes-3.0-flash", "effort": "high", "thinking": False}

async def main(action: str, bak_path: str):
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT key, value FROM system_config WHERE key LIKE 'model_layer.%'"))).all()
    bak = {}
    if action == "on":
        for key, raw in rows:
            try:
                bak[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
        pathlib.Path(bak_path).write_text(json.dumps(bak, ensure_ascii=False), encoding="utf-8")
        print(f"→ 已备份 {len(bak)} 个 model_layer 键原值到 {bak_path}")
    elif action == "off":
        # BUG 修复（2026-08-17）：off 必须从备份文件读回原值（原实现 cur 仍是当前值→写回无效）
        if not pathlib.Path(bak_path).exists():
            raise SystemExit(f"备份不存在（{bak_path}），无法还原——请手动恢复 model_layer.*")
        bak = json.loads(pathlib.Path(bak_path).read_text(encoding="utf-8"))
        print(f"✓ 已从备份读回 {len(bak)} 个 model_layer 键原值")
    updated = 0
    for key, raw in rows:
        if action == "on":
            try:
                cur = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            seg = cur.setdefault("employee", {})
            seg["llm"] = {**FREE}
            aux_usage = seg.get("llm_aux", {}).get("usage", ["kb_rank", "kb_summary", "memory_extract", "video", "resume"])
            seg["llm_aux"] = {**FREE, "usage": aux_usage}
        else:
            if key not in bak:
                continue
            cur = bak[key]
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE system_config SET value=:v, updated_at=NOW() WHERE key=:k"),
                {"v": json.dumps(cur, ensure_ascii=False), "k": key},
            )
        updated += 1
    print(f"✓ 已{'切换免费档' if action == 'on' else '还原'} {updated} 个 model_layer 键")
    await engine.dispose()
    print(f"✓ model_layer employee 段已{'切换免费档' if action == 'on' else '还原'}")

asyncio.run(main("$ACTION", "$BAK"))
PY

echo "== 重启 uvicorn =="
pkill -f "[u]vicorn app.main" || true
sleep 1
# 与 deploy/start.sh 对齐：TRUSTED_PROXIES 注入 + --no-proxy-headers（SEC-07 部署形态）
export TRUSTED_PROXIES=127.0.0.1
setsid nohup conda run -n aip uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-proxy-headers > /tmp/uvicorn.log 2>&1 < /dev/null &
for i in $(seq 1 15); do
  curl -s -m 2 -o /dev/null http://127.0.0.1:8000/api/v1/health && { echo "✓ uvicorn 已就绪（:8000）"; exit 0; }
  sleep 1
done
echo "✗ uvicorn 启动失败，日志见 /tmp/uvicorn.log"
exit 1
