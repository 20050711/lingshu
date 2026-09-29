"""MCP 外部工具接入迁移脚本（2026-09-03，幂等，可重复执行）。

改动：
1. mcp_tools 表加 url 列（Streamable HTTP 地址；空=规划展示未接入）
2. 幂等种子：MCP 外部工具默认条目

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_mcp.py
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine

_settings = get_settings()

DDL = [
    "ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS url VARCHAR(500)",
]


async def _check_port() -> None:
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", 8000))
        print("[ERROR] 后端 :8000 正在运行，请先停服再迁移（bash deploy/stop.sh --backend-only）")
        sys.exit(1)
    except OSError:
        pass
    finally:
        s.close()


async def main() -> None:
    await _check_port()
    engine = get_global_engine()
    async with engine.begin() as conn:
        for ddl in DDL:
            await conn.execute(text(ddl))
        # 2026-09-23（去游客态第 4 步）：删除 xhs_download 的幂等种子——
        # 该行早已由 migrate_xhs_merge 合并进单入口 xhs；游客链路整体下线后不再回种
    # 幂等校验
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT id, name, status, url FROM mcp_tools ORDER BY sort_order"))).all()
    print(f"迁移完成：mcp_tools 共 {len(rows)} 行")
    for r in rows:
        print(f"  - {r.id:14s} {r.status:10s} url={r.url or '(未接入)'}")


if __name__ == "__main__":
    asyncio.run(main())
