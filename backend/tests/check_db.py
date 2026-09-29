"""直查数据库：会话与消息对照。"""
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
                    "SELECT s.id, s.title, s.created_at::date,"
                    " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS msg_count"
                    " FROM sessions s ORDER BY s.created_at DESC LIMIT 6"
                )
            )
        ).all()
        for r in rows:
            print(str(r[0])[:8], repr(r[1]), r[2], "msg:", r[3])
        total = (await conn.execute(text("SELECT COUNT(*) FROM chat_messages"))).scalar()
        print("chat_messages 总数:", total)


if __name__ == "__main__":
    asyncio.run(main())
