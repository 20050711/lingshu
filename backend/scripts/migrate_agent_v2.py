"""v2 交互框架 · 数据库迁移脚本（幂等，可重复执行）。

1. sessions 加 3 列：mode（quick/complex 双模式）/ plan_json（批准卡计划 JSONB）/
   plan_revision_count（修订循环计数）
2. 存量数据回填：mode 默认 quick（server_default 已覆盖）；旧 D20 文字计划 pending 复位 none
   （v2 计划走批准卡，旧文字计划不再有确认通道）

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_agent_v2.py
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_global_engine


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


async def _column_exists(conn, table: str, column: str) -> bool:
    row = (await conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"
    ), {"t": table, "c": column})).first()
    return row is not None


async def main() -> int:
    if _port_in_use(8000):
        print("[ERROR] :8000 有后端在运行——请先停服再迁移（bash deploy/stop.sh 或 kill 后端进程）")
        return 1

    engine = get_global_engine()
    async with engine.begin() as conn:
        # 1. 加列（幂等）
        added: list[str] = []
        for name, ddl in [
            ("mode", "ALTER TABLE sessions ADD COLUMN mode VARCHAR(10) NOT NULL DEFAULT 'quick'"),
            ("plan_json", "ALTER TABLE sessions ADD COLUMN plan_json JSONB"),
            ("plan_revision_count", "ALTER TABLE sessions ADD COLUMN plan_revision_count INTEGER NOT NULL DEFAULT 0"),
        ]:
            if not await _column_exists(conn, "sessions", name):
                await conn.execute(text(ddl))
                added.append(name)
        print(f"加列: {added or '无（已存在）'}")

        # 2. 旧 D20 文字计划 pending → none（v2 批准卡取代；plan_text 列保留不删，旧行兼容）
        r = await conn.execute(text(
            "UPDATE sessions SET plan_status='none', plan_text=NULL, plan_round_id=NULL, "
            "plan_expires_at=NULL WHERE plan_status='pending'"
        ))
        print(f"旧 pending 计划复位: {r.rowcount} 行")

        # 3. 模式回填兜底（server_default 已保证，此处防手工改表场景）
        r2 = await conn.execute(text("UPDATE sessions SET mode='quick' WHERE mode IS NULL"))
        print(f"mode 回填: {r2.rowcount} 行")

        # 4. D3：批次级辅助模型覆盖（定制化工具页模型选择框；JSONB 存 {platform,model,effort?,thinking?}）
        for table in ("video_batches", "resume_batches"):
            if not await _column_exists(conn, table, "aux_model"):
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN aux_model JSONB"))
                print(f"加列: {table}.aux_model")

    print("=== migrate_agent_v2 完成 ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
