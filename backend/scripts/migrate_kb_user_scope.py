"""知识库归属三态迁移脚本（阶段 2 知识库改造，幂等，可重复执行）。

背景：知识库「归属三态」设计（2.0）——全局（department_id IS NULL AND user_id IS NULL）、
团队（department_id 非空 AND user_id IS NULL）、个人（user_id 非空，dept_id 恒 NULL）。
kb_documents / kb_categories 各加 user_id INT NULL 列 + 索引；**无回填**（NULL 即存量语义）。

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_kb_user_scope.py
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


async def _migrate_columns(conn) -> None:
    await conn.execute(text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS user_id INT"))
    await conn.execute(text("ALTER TABLE kb_categories ADD COLUMN IF NOT EXISTS user_id INT"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_kb_documents_user ON kb_documents (user_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_kb_categories_user ON kb_categories (user_id)"))
    print("✓ kb_documents / kb_categories 新增 user_id 列与索引就绪（NULL=存量全局/团队语义）")


async def main() -> None:
    await _check_port()
    engine = get_global_engine()
    async with engine.begin() as conn:
        print("== ① kb_documents / kb_categories 加 user_id 列 + 索引 ==")
        await _migrate_columns(conn)
    async with engine.connect() as conn:
        print("\n== 校验 ==")
        for table in ("kb_documents", "kb_categories"):
            cols = (
                await conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name=:t "
                         "AND column_name='user_id'"),
                    {"t": table},
                )
            ).all()
            idxs = (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename=:t AND indexname=:i"),
                    {"t": table, "i": f"idx_{table}_user"},
                )
            ).all()
            print(f"{table}: user_id 列={'✓' if cols else '✗'} / 索引={'✓' if idxs else '✗'}")
    print("\n迁移完成 ✅（可重复执行；无数据回填——NULL 即存量全局/团队语义）")


if __name__ == "__main__":
    asyncio.run(main())
