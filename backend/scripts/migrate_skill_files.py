"""团队技能文件迁移脚本（2026-08-20 zip 技能，幂等，可重复执行）。

skill_files 加列 skill_dir VARCHAR(255)：
- 存技能文件相对路径 "{dept_id}/{skill_id}"（md/ 与 scripts/ 分目录，根 = config.skill_files_dir=/data/skills）
- NULL = 存量纯文本技能（无磁盘文件），行为与迁移前一致，无需回填

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_skill_files.py
    # 部署机同步执行（代码推送后）：cd backend && conda run -n aip python scripts/migrate_skill_files.py
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


async def _migrate(conn) -> None:
    await conn.execute(text("ALTER TABLE skill_files ADD COLUMN IF NOT EXISTS skill_dir VARCHAR(255)"))
    print("✓ skill_files.skill_dir 列就绪（{dept_id}/{skill_id} 相对路径；NULL=纯文本技能）")
    # 2026-08-20 部署机事故修复：create_skill 走原生 SQL INSERT 不走 ORM default——
    # 新装环境表缺 DB 默认值 → status/sort_order NotNullViolation 500（开发机历史表带默认侥幸正常）
    await conn.execute(text("ALTER TABLE skill_files ALTER COLUMN status SET DEFAULT 'active'"))
    await conn.execute(text("ALTER TABLE skill_files ALTER COLUMN sort_order SET DEFAULT 0"))
    print("✓ skill_files.status/sort_order DB 默认值就绪（'active'/0）")


async def main() -> int:
    await _check_port()
    engine = get_global_engine()
    async with engine.begin() as conn:
        await _migrate(conn)
        # 校验列存在 + 统计存量
        col = (
            await conn.execute(
                text("SELECT column_name FROM information_schema.columns "
                     "WHERE table_name='skill_files' AND column_name='skill_dir'")
            )
        ).first()
        if not col:
            print("❌ skill_dir 列未生效，请检查日志")
            return 1
        cnt = (
            await conn.execute(text("SELECT COUNT(*) FROM skill_files WHERE skill_dir IS NOT NULL"))
        ).scalar()
        total = (await conn.execute(text("SELECT COUNT(*) FROM skill_files"))).scalar()
        print(f"✓ 存量技能 {total} 个（带文件目录 {cnt} 个，其余为纯文本技能不受影响）")
    await engine.dispose()
    print("=== migrate_skill_files done（部署机需同步执行本脚本）===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
