"""2026-08-10 设计检查全量修复 · 数据库迁移脚本（幂等，可重复执行）。

1. sessions 加 5 列（D20 计划确认状态机 + D27 原子轮号）：
   plan_status / plan_text / plan_round_id / plan_expires_at / last_round
2. last_round 回填（防存量会话新分配与旧 max+1 重叠）
3. 高频查询列补索引（E-06 数据层）：chart_outputs.session_id / chat_files.session_id /
   sessions.user_id / sessions.department_id / sync_log.dept_id / video_items.batch_id / resume_items.batch_id

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_plan.py
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


async def _migrate_session_plan(conn) -> None:
    """D20/D27：sessions 计划状态机列 + 原子轮号计数器。"""
    await conn.execute(
        text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS plan_status VARCHAR(20) NOT NULL DEFAULT 'none'")
    )
    await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS plan_text TEXT"))
    await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS plan_round_id INTEGER"))
    await conn.execute(text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS plan_expires_at TIMESTAMP"))
    await conn.execute(
        text("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_round INTEGER NOT NULL DEFAULT 0")
    )
    print("✓ sessions 已加 plan_status/plan_text/plan_round_id/plan_expires_at/last_round（D20/D27）")

    # last_round 回填（存量会话：取 chat_messages 最大 round_id；幂等——仅当当前为 0 时）
    await conn.execute(
        text(
            "UPDATE sessions SET last_round = COALESCE(("
            "  SELECT MAX(round_id) FROM chat_messages WHERE session_id = sessions.id"
            "), 0) WHERE last_round = 0"
        )
    )
    print("✓ sessions.last_round 已按存量消息回填")


async def _migrate_indexes(conn) -> None:
    """E-06：高频查询列补索引（幂等）。"""
    idxs = [
        ("idx_chart_session", "chart_outputs", "session_id"),
        ("idx_chatfiles_session", "chat_files", "session_id"),
        ("idx_sessions_user", "sessions", "user_id"),
        ("idx_sessions_dept", "sessions", "department_id"),
        ("idx_synclog_dept", "sync_log", "dept_id"),
        ("idx_videoitem_batch", "video_items", "batch_id"),
        ("idx_resumeitem_batch", "resume_items", "batch_id"),
    ]
    for name, table, col in idxs:
        await conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})")
        )
    print(f"✓ 已补齐 {len(idxs)} 个高频查询索引（E-06）")


async def main() -> None:
    await _check_port()
    engine = get_global_engine()
    async with engine.begin() as conn:
        print("== ① sessions 计划状态机与原子轮号（D20/D27）==")
        await _migrate_session_plan(conn)
        print("== ② 高频查询索引（E-06）==")
        await _migrate_indexes(conn)
    # 校验输出
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text("SELECT column_name FROM information_schema.columns "
                     "WHERE table_schema='public' AND table_name='sessions' "
                     "AND column_name IN ('plan_status','plan_text','plan_round_id','plan_expires_at','last_round')")
            )
        ).all()
        idxs = (
            await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'idx_%' "
                     "AND indexname IN ('idx_chart_session','idx_chatfiles_session','idx_sessions_user',"
                     "'idx_sessions_dept','idx_synclog_dept','idx_videoitem_batch','idx_resumeitem_batch')")
            )
        ).all()
        print("\n== 校验 ==")
        print("sessions 新列:", [c[0] for c in cols])
        print("新索引:", [i[0] for i in idxs])
    print("迁移完成 ✅（可重复执行）")


if __name__ == "__main__":
    asyncio.run(main())
