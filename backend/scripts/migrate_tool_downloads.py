"""工具下载包迁移脚本（2026-09-04，幂等，可重复执行）。

建 1 张全局库表（IF NOT EXISTS，不动现有表）：
tool_downloads：运维上传的离线工具包（title/desc/filename/stored_path/size/enabled/sort_order）

存量兼容：首次执行时把 {tool_downloads_dir} 目录内已存在的 *.zip 自动导入为行
（title=文件名去后缀，desc=空，enabled=true）——早前"目录扫描即出现"阶段的文件无缝上版。

用法（先停 uvicorn 再执行）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_tool_downloads.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine

DDL = [
    """CREATE TABLE IF NOT EXISTS tool_downloads (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        title VARCHAR(100) NOT NULL,
        description TEXT,
        filename VARCHAR(200) NOT NULL,
        stored_path VARCHAR(260) NOT NULL,
        size BIGINT NOT NULL DEFAULT 0,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )""",
]


async def main() -> None:
    engine = get_global_engine()
    async with engine.begin() as conn:
        for ddl in DDL:
            await conn.execute(text(ddl))
        # 存量：目录内 zip 导入（stored_path=原文件名；已存在同名 stored_path 跳过）
        d = Path(get_settings().tool_downloads_dir)
        imported = 0
        if d.is_dir():
            for p in sorted(d.glob("*.zip")):
                exists = (await conn.execute(
                    text("SELECT 1 FROM tool_downloads WHERE stored_path=:s"), {"s": p.name})).first()
                if exists:
                    continue
                await conn.execute(
                    text("INSERT INTO tool_downloads (title, description, filename, stored_path, size) "
                         "VALUES (:t, :d, :f, :s, :z)"),
                    {"t": p.stem, "d": "", "f": p.name, "s": p.name, "z": p.stat().st_size})
                imported += 1
        print(f"迁移完成：tool_downloads 表就绪" + (f"；存量导入 {imported} 个" if imported else ""))


if __name__ == "__main__":
    asyncio.run(main())
