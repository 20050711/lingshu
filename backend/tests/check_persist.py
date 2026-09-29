"""用真实会话 ID 直接测 persist_round，暴露被吞的异常。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_global_engine
from app.services.chat_service import persist_round


async def main() -> None:
    engine = get_global_engine()
    async with engine.connect() as conn:
        sid = (
            await conn.execute(
                text("SELECT id FROM sessions ORDER BY created_at DESC LIMIT 1")
            )
        ).first()[0]
    print("真实 session:", sid)
    try:
        await persist_round(
            str(sid), 1, "测试问题",
            {"messages": [{"role": "assistant", "content": "测试回答"}], "round_outputs": []},
        )
        print("persist_round OK")
    except Exception as e:
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
