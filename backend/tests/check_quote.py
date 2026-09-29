"""查最近会话中带删除线嫌疑的原始消息文本。"""
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
                    "SELECT m.content FROM chat_messages m JOIN sessions s ON m.session_id = s.id"
                    " WHERE m.role='assistant' ORDER BY m.created_at DESC LIMIT 5"
                )
            )
        ).all()
    for i, (content,) in enumerate(rows):
        if content and ("~~" in content or "2.42" in content):
            # 打印包含 ~~ 或 2.42 的片段
            for marker in ("~~", "2.42"):
                idx = content.find(marker)
                if idx >= 0:
                    print(f"[{i}] 命中 {marker!r}: ...{content[max(0, idx-60):idx+80]}...")
        else:
            print(f"[{i}] (无命中, 长度 {len(content or '')})")


if __name__ == "__main__":
    asyncio.run(main())
