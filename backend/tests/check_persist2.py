"""验证：chat_messages 落库了 tool_events 与 files。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_global_engine


async def main() -> None:
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT role, COALESCE(tool_events::text, '-'), COALESCE(files::text, '-')"
                    " FROM chat_messages ORDER BY created_at DESC LIMIT 4"
                )
            )
        ).all()
        for r in rows:
            print(r[0], "| tool_events:", (r[1] or "")[:90], "| files:", (r[2] or "")[:60])


if __name__ == "__main__":
    asyncio.run(main())
