"""断连降级落库复现测试（2026-08-10 根因验证）。

历史遗留问题：客户端断连 → 该轮 user 消息不落库 → 刷新后消息消失。
根因：uvicorn cancel scope → 生成器收 CancelledError → 任务取消状态下 except 内 await
立即再抛 CancelledError → _persist_degraded 永不执行。修复：uncancel + 降级落库防再抛。

本测试真实模拟：迭代 stream_ask 生成器 → 中途 cancel（模拟客户端断连）
→ 断言 chat_messages 中 user 消息已降级落库。

用法: cd backend && source scripts/env_aip.sh && python -u tests/test_disconnect_persist.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

# 2026-09-17：本测试**进程内**跑图（legacy_stream_ask），不设此开关会真实打 DeepSeek——
# 默认强制模拟档（零费用）；确需真实 LLM 时显式 LLM_MOCK=0 启动。
os.environ.setdefault("LLM_MOCK", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


async def main() -> int:
    from sqlalchemy import text

    from app.core.database import get_global_engine
    from app.services.chat_service import legacy_stream_ask  # 2026-08-18：任务后台化后旧路径改名保留

    engine = get_global_engine()
    sid = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO sessions (id, user_id, client_id, title, department_id, is_readonly) "
                 "VALUES (:id, 1, 'smoke', '断连测试', 'demo', false)"),
            {"id": sid},
        )
    user = {"user_id": 1, "role": "employee", "dept_id": "demo", "username": "smoke"}

    async def consume():
        """迭代 stream_ask 生成器（不主动退出——等待外部 cancel 模拟客户端断连）。"""
        gen = legacy_stream_ask(session_id=sid, round_id=1, question="断连测试问题",
                         user=user, active_skills=[], auto_skill=False, multi_table=False,
                         client_id="smoke")
        async for _frame in gen:
            pass

    try:
        task = asyncio.create_task(consume())
        # 等生成器启动（agent 开始跑），3 秒后 cancel 消费任务 = 模拟客户端断连
        # （uvicorn cancel scope 对生成器消费任务取消 → CancelledError 注入 stream_ask）
        await asyncio.sleep(3)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        # 断言：user 消息已降级落库（round 1）
        async with engine.connect() as conn:
            rows = (await conn.execute(
                text("SELECT role, content FROM chat_messages WHERE session_id=:s AND round_id=1"),
                {"s": sid})).all()
        # 2026-09-10（脆弱断言修复）：原为 `r[1] == "断连测试问题"` 精确相等——但应用会合法地给
        # user 消息**前置上下文块**（`【上下文开始】…【记忆上下文】…`，团队/user 记忆存在时注入），
        # 于是本用例随环境状态时好时坏（免费档 PASS、有团队记忆时 FAIL）。测试意图是"落库了"，
        # 改为包含判定（注入块是既有功能，远早于本次改动）。
        check("断连后 user 消息落库",
              any(r[0] == "user" and "断连测试问题" in (r[1] or "") for r in rows),
              f"rows={[(r[0], str(r[1])[:20]) for r in rows]}")
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM chat_messages WHERE session_id=:s"), {"s": sid})
            await conn.execute(text("DELETE FROM sessions WHERE id=:s"), {"s": sid})

    print(f"=== {'PASS' if FAIL == 0 else 'FAIL'}（{PASS} 过 / {FAIL} 败）===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
