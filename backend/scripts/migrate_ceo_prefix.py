"""tardis_ceo_db 表名前缀迁移（三期 M16）：裸表 RENAME 为 {dept}_ 前缀（幂等）。

背景：M16 起 tardis_ceo_db 表名统一 {dept}_{table}（多团队同表名冲突的必然解）。
本脚本把存量裸表（market 同步的 biji 等）重命名为 market_*，并清空旧 _schema_meta
（后续手动 ceo_sync 会重建带前缀的合并元数据）。

用法（conda 环境）：
    cd backend && python -m scripts.migrate_ceo_prefix
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import get_dept_engine
from sqlalchemy import text

PREFIX = "market"  # 存量数据的团队（当前仅 market 一团队有 ceo 快照）


async def migrate() -> None:
    ceo = get_dept_engine("ceo")
    async with ceo.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT table_name FROM information_schema.tables "
                     "WHERE table_schema='public' AND table_name != '_schema_meta' AND "
                     "table_name NOT LIKE :p ORDER BY table_name"),
                {"p": f"{PREFIX}_%"},
            )
        ).all()
    renamed = 0
    async with ceo.begin() as conn:
        for r in rows:
            t = r[0]
            target = f"{PREFIX}_{t}"
            # 目标已存在（前缀表）则跳过，避免覆盖
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:t"),
                    {"t": target},
                )
            ).first()
            if exists:
                print(f"skip {t}（{target} 已存在）")
                continue
            await conn.execute(text(f'ALTER TABLE "{t}" RENAME TO "{target}"'))
            renamed += 1
            print(f"renamed: {t} → {target}")
        # 清空旧 _schema_meta（无前缀行），由后续 ceo_sync 重建
        await conn.execute(text("DROP TABLE IF EXISTS _schema_meta"))
    await ceo.dispose()
    print(f"=== 迁移完成：{renamed} 张表加前缀；_schema_meta 已清空（重新 ceo_sync 后重建）===")


if __name__ == "__main__":
    asyncio.run(migrate())
