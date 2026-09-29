"""验证清库结果（只读报告，无断言/退出码）。

2026-09-17：数据查询线下线后**团队库/表原地保留**（决策②"改动小一点"）——
因此"业务表残留"不再是异常，本脚本仅打印现状供人工比对。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_dept_engine, get_global_engine


async def main() -> None:
    g = get_global_engine()
    async with g.connect() as c:
        s = (await c.execute(text("SELECT COUNT(*) FROM sessions"))).scalar()
        m = (await c.execute(text("SELECT COUNT(*) FROM chat_messages"))).scalar()
        print(f"全局库: sessions={s} chat_messages={m}")
    d = get_dept_engine("demo")
    async with d.connect() as c:
        t = (
            await c.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables"
                    " WHERE table_name IN ('toufang_leixing','chengshi_leixing','biji','shiduan','sousuoci','renqunbao','_schema_meta')"
                )
            )
        ).scalar()
        print(f"团队库(demo)业务表存在: {t}/7（2026-09-17 起允许保留——数据查询线已下线，库留作存档）")


if __name__ == "__main__":
    asyncio.run(main())
