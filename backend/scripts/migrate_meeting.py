"""会议纪要工具模块迁移脚本（2026-08-25，幂等，可重复执行）。

meeting_recordings：录音记录（上传 → 转写 → 场景总结 → zip 下载）

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_meeting.py
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


async def _create_meeting_table(conn) -> None:
    await conn.execute(
        text("""
            CREATE TABLE IF NOT EXISTS meeting_recordings (
              id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
              user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title VARCHAR(200),
              server_path VARCHAR(500),
              duration_s FLOAT,
              status VARCHAR(20) NOT NULL DEFAULT 'uploaded',
              transcript TEXT,
              transcript_path VARCHAR(500),
              summary TEXT,
              summary_path VARCHAR(500),
              scene VARCHAR(50),
              custom_prompt TEXT,
              error_msg TEXT,
              dept_id VARCHAR(50),
              user_role VARCHAR(20),
              created_at TIMESTAMP NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )""")
    )


async def main() -> int:
    await _check_port()
    engine = get_global_engine()
    async with engine.begin() as conn:
        await _create_meeting_table(conn)
        # 2026-08-25 双轨录音：线上轨（系统声音）路径列 + 合成音频列（幂等）
        await conn.execute(text("ALTER TABLE meeting_recordings ADD COLUMN IF NOT EXISTS sys_path VARCHAR(500)"))
        await conn.execute(text("ALTER TABLE meeting_recordings ADD COLUMN IF NOT EXISTS merged_path VARCHAR(500)"))
    print("meeting_recordings 表已就绪（幂等）")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
