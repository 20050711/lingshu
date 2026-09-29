"""任务后台化服务层测试（2026-08-18）：start_bg_task/tail_stream 直驱，不依赖 HTTP。

用例：
1. tail_stream 迭代若干帧后 aclose()（模拟客户端断连）→ agent 任务未取消、注册表仍在
   → 重新 tail_stream(rec, last_seq) 续播 → seq 严格递增无重复、done 到达、
   chat_messages 恰好 1 user + 1 assistant（persist_round 只执行一次）、Redis 事件流存在
2. 停止路径：terminated.set() + agent_task.cancel() → aborted 事件入流 + 降级落库 + task_status=interrupted

用法: cd backend && source scripts/env_aip.sh && LLM_MOCK=1 python -u tests/test_bg_task_reconnect.py
注意：依赖 mock 的多轮流程（工具集需含 file_search，见 llm_client._mock_ainvoke）——
file_search 恒注入，显式给 active_skills 只为稳定复现。
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


async def _mk_session() -> str:
    from sqlalchemy import text

    from app.core.database import get_global_engine

    sid = str(uuid.uuid4())
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO sessions (id, user_id, client_id, title, department_id, is_readonly) "
                 "VALUES (:id, 1, 'smoke', '后台化测试', 'demo', false)"),
            {"id": sid},
        )
    return sid


async def _collect(gen) -> list[dict]:
    """迭代 tail_stream 生成器，返回 [{event, data}...]。"""
    frames = []
    async for frame in gen:
        ev = ""
        payload: dict = {}
        for line in frame.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                import json

                try:
                    payload = json.loads(line[6:])
                except Exception:
                    payload = {}
        if ev == "heartbeat":
            continue
        frames.append({"event": ev, **payload})
    return frames


async def main() -> int:
    from sqlalchemy import text

    from app.core.database import get_global_engine
    from app.services.chat_service import start_bg_task, tail_stream
    from app.services.task_registry import get_task

    engine = get_global_engine()
    user = {"user_id": 1, "role": "employee", "dept_id": "demo", "username": "smoke"}
    question = "你好，请简单打个招呼"

    # ===== 用例 1：断连 → 后台继续 → 重连续播 =====
    print("== 用例 1：断连重连续播 ==")
    sid = await _mk_session()
    rec = await start_bg_task(
        session_id=sid, round_id=1, question=question, user=user,
        # 2026-09-17：mock LLM 的多轮流程锚定 file_search（见 llm_client._mock_ainvoke
        # 「阶段 1」），否则一轮直答 → 任务无"仍在跑"窗口，用例前提不成立。
        # 同一坑 2026-08-19 已修过 e2e_reconnect，此处漏修——对齐同法。
        active_skills=["file_parse"], auto_skill=False, multi_table=False, client_id="smoke",
    )
    # 首连：迭代到 3 帧后 aclose（模拟断连）
    gen = tail_stream(rec, -1)
    first: list[dict] = []
    async for frame in gen:
        import json

        ev = ""
        payload: dict = {}
        for line in frame.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except Exception:
                    payload = {}
        if ev != "heartbeat":
            first.append({"event": ev, **payload})
        if len(first) >= 3:
            break
    await gen.aclose()  # 模拟客户端断连（GeneratorExit 注入）
    check("首连收到 ≥3 帧", len(first) >= 3)
    last_seq = max((f.get("seq", -1) for f in first if isinstance(f.get("seq"), int)), default=-1)
    check("首连未到终态（断连时任务仍在跑）",
          not any(f["event"] in ("done", "error", "aborted") for f in first))

    # 断连后任务应继续（agent 未取消、注册表仍在）
    rec2 = get_task(sid)
    check("断连后注册表任务仍在", rec2 is not None and rec2.status == "running")
    check("断连后 agent_task 未取消", rec2 is not None and not rec2.agent_task.cancelled())

    await asyncio.sleep(2)
    gen2 = tail_stream(rec2, last_seq)
    second = await _collect(gen2)
    seqs = [f.get("seq") for f in second if isinstance(f.get("seq"), int)]
    evs = [f["event"] for f in second]
    print(f"[重连] {len(second)} 帧 events={evs[:12]} seqs={seqs[:12]}")
    check("重连 seq 从 last_seq+1 起", len(seqs) > 0 and seqs[0] == last_seq + 1)
    check("重连 seq 严格递增无重复", len(set(seqs)) == len(seqs) and seqs == sorted(seqs))
    check("重连以 done 收尾", "done" in evs)
    # 跨连接全集无重复
    all_seq = [f.get("seq") for f in first if isinstance(f.get("seq"), int)] + seqs
    check("跨连接全集 seq 无重复", len(set(all_seq)) == len(all_seq))
    # persist_round 恰好一次：1 user + 1 assistant
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT role FROM chat_messages WHERE session_id=:s ORDER BY created_at"),
                {"s": sid},
            )
        ).all()
    roles = [r[0] for r in rows]
    check("落库恰 1 user + 1 assistant（persist 只一次）",
          roles.count("user") == 1 and roles.count("assistant") == 1, f"roles={roles}")
    # 事件流存在
    from app.core.redis import redis_llen

    llen = await redis_llen(f"sse_events:{sid}")
    check("Redis 事件流存在且含终态条目", llen > 0)

    # ===== 用例 2：显式停止 → aborted 入流 + interrupted =====
    print("== 用例 2：显式停止 ==")
    sid2 = await _mk_session()
    rec3 = await start_bg_task(
        session_id=sid2, round_id=1, question=question, user=user,
        # 2026-09-17：mock LLM 的多轮流程锚定 file_search（见 llm_client._mock_ainvoke
        # 「阶段 1」），否则一轮直答 → 任务无"仍在跑"窗口，用例前提不成立。
        # 同一坑 2026-08-19 已修过 e2e_reconnect，此处漏修——对齐同法。
        active_skills=["file_parse"], auto_skill=False, multi_table=False, client_id="smoke",
    )
    await asyncio.sleep(1)
    rec3.terminated.set()
    rec3.agent_task.cancel()
    await asyncio.sleep(2)  # 等 relay 收尾（aborted 写流）
    gen3 = tail_stream(rec3, -1)
    replay = await _collect(gen3)
    evs3 = [f["event"] for f in replay]
    print(f"[停止回放] events={evs3}")
    check("停止后回放含 aborted", "aborted" in evs3)
    check("停止后注册表已清理", get_task(sid2) is None)
    async with engine.connect() as conn:
        st = (
            await conn.execute(text("SELECT task_status FROM sessions WHERE id=:s"), {"s": sid2})
        ).first()
    check("停止后 task_status=interrupted", st is not None and st[0] == "interrupted", f"got={st}")

    # 清理
    from app.core.redis import redis_delete

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM sessions WHERE id IN (:a, :b)"), {"a": sid, "b": sid2})
    await redis_delete(f"sse_events:{sid}", f"sse_events:{sid2}")
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
