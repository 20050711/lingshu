"""会话上下文压缩冒烟（2026-09-15：进度条 + 手动/被动压缩）。

覆盖：
1. latest_round / session_context 结构（compactible / 水位）
2. compact_session：边界=最新轮-保留轮数 / 摘要落 Redis / 水位重算 / 幂等拒绝（无可压缩内容）
3. 滚动压缩：第二次压缩输入 = 上次摘要 + 仅新增轮次（capture _summarize 入参断言）
4. _load_history 集成：≤boundary 原文不再注入、摘要块注入、保留轮原文仍在
5. 互斥锁：二次获取失败、释放后可再取
6. **真实链路（GLM 免费档 1 次调用）**：真实压缩产出五段式结构化摘要

用法：cd backend && source scripts/env_aip.sh && python -u tests/context_compact_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, insert  # noqa: E402

from app.core.database import get_global_engine  # noqa: E402
from app.core.redis import redis_delete  # noqa: E402
from app.models import ChatMessage, Session  # noqa: E402
from app.services import context_service as ctx  # noqa: E402

_RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


async def _seed(session_id: str, rounds: int, start_rid: int = 1) -> None:
    """播种轮次：每轮 user 问题 + assistant 答复（含产出与工具事件）。"""
    engine = get_global_engine()
    outputs = json.dumps([{"type": "doc", "label": "分析报告", "file_path": f"/data/outputs/{session_id}/1/report.docx"}],
                         ensure_ascii=False)
    tools = json.dumps([{"tool_name": "file_parse", "status": "done", "summary": "解析渠道数据 4033 行"}],
                       ensure_ascii=False)
    async with engine.begin() as conn:
        for rid in range(start_rid, start_rid + rounds):
            await conn.execute(insert(ChatMessage).values(
                session_id=session_id, role="user", content=f"第{rid}轮用户问题：请分析渠道数据", round_id=rid))
            await conn.execute(insert(ChatMessage).values(
                session_id=session_id, role="assistant", round_id=rid,
                content=f"第{rid}轮答复：转化率 12.3%，产出分析报告。", outputs=outputs, tool_events=tools))


async def main() -> int:
    session_id = str(uuid.uuid4())
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(insert(Session).values(
            id=session_id, user_id=2, client_id="smoke", title="压缩冒烟", department_id="demo"))
    keys = [f"ctxsum:v1:{session_id}", f"ctxstat:v1:{session_id}", f"ctxlock:v1:{session_id}",
            f"interrupted:{session_id}", f"sse_events:{session_id}"]
    orig_summarize = ctx._summarize
    try:
        await _seed(session_id, 7)  # 1-7 轮
        latest = await ctx.latest_round(session_id)
        record("latest_round=7", latest == 7, str(latest))
        sc = await ctx.session_context(session_id)
        record("compactible=True（7 轮 > 保留 3）", sc["compactible"] is True, str(sc["compactible"]))
        record("初始水位 0 / 无摘要", sc["watermark_tokens"] == 0 and not sc["summary"])

        captured: dict = {}

        async def _fake_summarize(user_input: str, dept_id, user_role) -> str:
            captured["input"] = user_input
            return ("## 目标与背景\n渠道数据分析\n## 已完成\n7 轮分析\n## 关键数据\n转化率 12.3%\n"
                    "## 未决 / 待办\n无\n## 参考物（路径）\n/data/outputs/x/report.docx")

        ctx._summarize = _fake_summarize  # type: ignore[assignment]
        # 模拟一次 ask 后的水位（base=1000 / history=2000 / real=2800）
        await ctx.save_stat(session_id, {"history": 2000, "total": 3000, "real": 2800})

        r = await ctx.compact_session(session_id, "demo", "employee")
        record("压缩成功 boundary=4（7-3）", bool(r.get("ok")) and r.get("boundary") == 4,
               str(r.get("error") or r.get("boundary")))
        record("摘要落 Redis", (await ctx.get_summary(session_id) or {}).get("boundary") == 4)
        record("水位重算下降", 0 < int(r.get("watermark_tokens") or 0) < 2800,
               f"2800 → {r.get('watermark_tokens')}")

        r2 = await ctx.compact_session(session_id, "demo", "employee")
        record("无新内容时幂等拒绝", (not r2.get("ok")) and "没有可压缩" in str(r2.get("error")),
               str(r2.get("error"))[:60])

        from app.services.chat_service import _load_history

        msgs, _, _ = await _load_history(session_id, dept_id="demo", user_role="employee")
        joined = "\n".join(m.get("content") or "" for m in msgs)
        record("_load_history 注入压缩摘要块", "【会话压缩摘要（第 1-4 轮，已压缩）】" in joined)
        record("≤boundary 原文不再注入", "第1轮答复" not in joined and "第4轮答复" not in joined)
        record("保留轮（5-7）原文仍在", "第5轮答复" in joined and "第7轮答复" in joined)

        await _seed(session_id, 4, start_rid=8)  # 8-11 轮
        r3 = await ctx.compact_session(session_id, "demo", "employee")
        record("滚动压缩 boundary=8（11-3）", bool(r3.get("ok")) and r3.get("boundary") == 8, str(r3.get("boundary")))
        record("第二次输入含上次摘要", "上一次压缩摘要" in captured.get("input", ""))
        record("第二次输入仅含新增轮次（5-8）",
               "第5轮用户问题" in captured.get("input", "") and "第4轮用户问题" not in captured.get("input", ""))

        got1 = await ctx.acquire_lock(session_id)
        got2 = await ctx.acquire_lock(session_id)
        await ctx.release_lock(session_id, "wrong-token")  # 错误 token 不误删（自冲突审计）
        still_locked = await ctx.compacting(session_id)
        await ctx.release_lock(session_id, got1)
        got3 = await ctx.acquire_lock(session_id)
        await ctx.release_lock(session_id, got3)
        record("互斥锁：二次获取被拒 / 错误 token 不误删 / 正确 token 可释放",
               bool(got1) and (got2 is None) and still_locked and bool(got3),
               f"{bool(got1)}/{got2}/{still_locked}/{bool(got3)}")

        # 并发压缩（自冲突审计）：三个触发入口同刻竞争 → 锁保证只有一个真正执行
        await _seed(session_id, 4, start_rid=12)  # 12-15 轮 → 新 boundary=12

        async def _try_compact() -> str:
            t = await ctx.acquire_lock(session_id)
            if not t:
                return "locked"
            try:
                rr = await ctx.compact_session(session_id, "demo", "employee", reason="race-test")
                return "ok" if rr.get("ok") else "skip"
            finally:
                await ctx.release_lock(session_id, t)

        race = await asyncio.gather(*(_try_compact() for _ in range(3)))
        record("并发三连压：仅 1 个执行、其余被锁拒", race.count("ok") == 1 and race.count("locked") == 2, str(race))
        record("并发后摘要仍一致（boundary=12）", (await ctx.get_summary(session_id) or {}).get("boundary") == 12)

        # 真实链路：GLM 免费档（llm_aux 配置临时指向 glm-4.7-flash）
        from app.services import config_service

        orig_cfg = config_service.get_aux_model_cfg_for_task

        async def _free_cfg(dept_id, role, task):
            return {"platform": "glm", "model": "glm-4.7-flash"}

        config_service.get_aux_model_cfg_for_task = _free_cfg  # type: ignore[assignment]
        ctx._summarize = orig_summarize
        try:
            await _seed(session_id, 3, start_rid=16)  # 16-18 轮 → boundary=15
            r4 = await ctx.compact_session(session_id, "demo", "employee", reason="smoke-real")
            summ = str(r4.get("summary") or "")
            ok = bool(r4.get("ok")) and "## 目标与背景" in summ and "## 参考物" in summ
            record("真实 GLM 压缩产出五段式摘要", ok, f"{len(summ)}字 {summ[:70]!r}")
        finally:
            config_service.get_aux_model_cfg_for_task = orig_cfg

        # 7. 被动触发（提问前水位 ≥ 50%）：直接驱动 _maybe_compact_before_ask
        from app.services.chat_service import _maybe_compact_before_ask
        from app.services.events import EventBroadcaster
        from app.services.task_registry import TaskRecord

        await _seed(session_id, 2, start_rid=19)  # 19-20 轮 → 新 boundary=17
        await ctx.save_stat(session_id, {"history": 120000, "total": 130000, "real": 125000})  # 水位 62.5%
        bc = EventBroadcaster(session_id, 60)
        rec = TaskRecord(session_id=session_id, round_id=21, question="被动压缩冒烟", broadcaster=bc)
        initial: dict = {"messages": [{"role": "user", "content": "旧历史占位"}], "tool_events": []}
        ctx._summarize = _fake_summarize  # type: ignore[assignment]  # 被动触发测试不花 LLM
        try:
            await _maybe_compact_before_ask(rec, initial)
        finally:
            ctx._summarize = orig_summarize  # type: ignore[assignment]
        events = []
        while not bc.queue.empty():
            events.append(bc.queue.get_nowait())
        joined = "\n".join(str(m.get("content") or "") for m in initial["messages"])
        new_boundary = (await ctx.get_summary(session_id) or {}).get("boundary")
        record("被动触发：水位≥50% 提问前自动压缩并重载历史",
               new_boundary == 17 and "【会话压缩摘要（第 1-17 轮，已压缩）】" in joined
               and any(e.get("tool_name") == "context_compact" and e.get("status") == "done" for e in events)
               and initial.get("tool_events"),
               f"boundary={new_boundary} events={len(events)} 历史已重载={'【会话压缩摘要' in joined}")
        # 低于阈值时不触发（回归：不会每次提问都压）
        await ctx.save_stat(session_id, {"history": 1000, "total": 2000, "real": 1500})  # 0.75%
        before = await ctx.get_summary(session_id)
        await _maybe_compact_before_ask(rec, initial)
        record("低于阈值不触发（幂等安全）", (await ctx.get_summary(session_id)) == before)

        sc2 = await ctx.session_context(session_id)
        record("session_context 反映最新 boundary=17 与摘要", sc2.get("boundary") == 17 and bool(sc2.get("summary")),
               str(sc2.get("boundary")))
    finally:
        ctx._summarize = orig_summarize  # type: ignore[assignment]
        async with engine.begin() as conn:
            await conn.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
            await conn.execute(delete(Session).where(Session.id == session_id))
        await redis_delete(*keys)

    failed = [x for x in _RESULTS if not x[1]]
    print(f"\n== {len(_RESULTS) - len(failed)}/{len(_RESULTS)} PASS ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
