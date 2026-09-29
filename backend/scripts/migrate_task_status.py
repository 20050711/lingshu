"""会话任务状态迁移脚本（任务后台化 2026-08-18，幂等，可重复执行）。

sessions 加列 task_status（none/running/completed/error/interrupted），
供「服务重启后 running 任务标记中断」的 lifespan 恢复使用。

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_task_status.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_global_engine


async def _check_port() -> None:
    import socket

    s = socket.socket()
    try:
        s.connect(("127.0.0.1", 8000))
        print("⚠️  检测到后端正在运行（8000 端口）。请先停 uvicorn 再执行迁移！")
        sys.exit(1)
    except OSError:
        pass
    finally:
        s.close()


async def _migrate() -> None:
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS "
                 "task_status VARCHAR(20) NOT NULL DEFAULT 'none'")
        )
    # 校验
    async with engine.connect() as conn:
        col = (
            await conn.execute(
                text("SELECT column_name FROM information_schema.columns "
                     "WHERE table_name='sessions' AND column_name='task_status'")
            )
        ).first()
    if col:
        print("✓ sessions.task_status 列就绪")
    else:
        print("✗ 迁移失败：sessions.task_status 列未创建")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_check_port())
    asyncio.run(_migrate())
    print("迁移完成。")
