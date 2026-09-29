"""迁移：sessions.cost_tokens 列（需求 3 token 计费，2026-08-17）。幂等可重跑。

用法: source scripts/env_aip.sh && python scripts/migrate_cost.py
"""
import asyncio
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.core.database import get_global_engine  # noqa: E402


async def main() -> None:
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cost_tokens INTEGER NOT NULL DEFAULT 0"
        ))
        # 2026-08-17（价格换算）：细分列
        await conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cost_prompt_hit INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cost_prompt_miss INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cost_completion INTEGER NOT NULL DEFAULT 0"
        ))
        await conn.execute(text(
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cost_model VARCHAR(64)"
        ))
        # 2026-08-17（双模型设计）：视觉模型列（与文本模型分开）
        await conn.execute(text(
            "ALTER TABLE video_batches ADD COLUMN IF NOT EXISTS aux_vision_model JSONB"
        ))
    await engine.dispose()
    print("✓ sessions 计费列 + video_batches.aux_vision_model 列就绪（幂等）")


if __name__ == "__main__":
    asyncio.run(main())
