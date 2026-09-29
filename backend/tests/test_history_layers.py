"""支柱 2 单测（2026-08-10）：历史分层压缩——超预算触发摘要注入 / 短会话零调用 / 缓存复用。

用法: cd backend && source scripts/env_aip.sh && python -u tests/test_history_layers.py
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

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
    import app.services.chat_service as cs
    from app.core.config import get_settings
    from app.core.database import get_global_engine
    from sqlalchemy import text

    settings = get_settings()
    engine = get_global_engine()
    sid = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO sessions (id, user_id, client_id, title, department_id, is_readonly) "
                 "VALUES (:id, 1, 'smoke', '冒烟', 'demo', false)"),
            {"id": sid},
        )

    calls = {"n": 0}
    captured = {}
    fake_cache: dict = {}

    async def fake_summary(session_id, dropped, dept_id, user_role):
        # 模拟真实实现的 Redis 缓存（key=session+最新被丢轮次）：同 boundary 复用不重算
        key = (session_id, max(r for r, _ in dropped))
        if key in fake_cache:
            return fake_cache[key]
        calls["n"] += 1
        captured["dropped"] = [r for r, _ in dropped]
        fake_cache[key] = f"【摘要】共 {len(dropped)} 轮被压缩：完成解析模板"
        return fake_cache[key]

    original = cs._build_history_summary
    cs._build_history_summary = fake_summary
    try:
        try:
            # 短会话（小消息）：不应触发摘要
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO chat_messages (session_id, role, content, round_id) "
                         "VALUES (:s, 'user', '问题一', 1), (:s, 'assistant', '回答一', 1), "
                         "(:s, 'user', '问题二', 2), (:s, 'assistant', '回答二', 2)"),
                    {"s": sid},
                )
            msgs, _, _ = await cs._load_history(sid, max_rounds=10, dept_id="demo", user_role="employee")
            check("短会话零摘要调用", calls["n"] == 0 and len(msgs) == 4, f"calls={calls['n']} msgs={len(msgs)}")
            check("短会话无摘要头", not any("已压缩" in (m.get("content") or "") for m in msgs), "")

            # 长会话（每轮 60K 字符 → 2 轮超原文层预算 105K）→ 触发摘要注入
            calls["n"] = 0
            async with engine.begin() as conn:
                for i in (3, 4):
                    await conn.execute(
                        text("INSERT INTO chat_messages (session_id, role, content, round_id) "
                             "VALUES (:s, 'user', :c, :r), (:s, 'assistant', '答', :r)"),
                        {"s": sid, "c": "长" * 60000, "r": i},
                    )
            msgs2, _, _ = await cs._load_history(sid, max_rounds=10, dept_id="demo", user_role="employee")
            check("超预算触发摘要", calls["n"] >= 1, f"calls={calls['n']}")
            check("摘要注入头部", msgs2 and "已压缩" in (msgs2[0].get("content") or ""), str(msgs2[0].get("content"))[:50] if msgs2 else "")
            check("原文层保留最新轮", any(m.get("content") == "长" * 60000 for m in msgs2), "")

            # 缓存复用：同一 boundary 第二次调用不再触发摘要
            calls["n"] = 0
            await cs._load_history(sid, max_rounds=10, dept_id="demo", user_role="employee")
            check("摘要缓存复用", calls["n"] == 0, f"calls={calls['n']}（缓存命中不重复调用）")
        finally:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM chat_messages WHERE session_id=:s"), {"s": sid})
                await conn.execute(text("DELETE FROM sessions WHERE id=:s"), {"s": sid})
    finally:
        cs._build_history_summary = original

    print(f"=== {'PASS' if FAIL == 0 else 'FAIL'}（{PASS} 过 / {FAIL} 败）===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
