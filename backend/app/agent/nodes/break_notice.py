"""平台中断说明生成（2026-09-08，护栏重构配套）。

强制收尾（无进展/失败兜底/卡点收尾）时由**系统直接生成**两分类说明，不经 LLM——
原行为让 LLM 生成收尾文本：要么被"工具标记拦截"整段作废（用户看到无信息的
"任务已完成，请查看上方产出与工具执行情况。"），要么把等待型失败误述成"任务完成"。

用户决策（2026-09-08）：说明分两类（有产出/无产出）、系统直发、不弹确认卡、等用户发话；
文本带【平台说明】前缀（前端灰色样式区分），并引导"回复「继续」从断点续跑"。
"""
from __future__ import annotations

import json
import re

PLATFORM_TAG = "【平台说明】"


def _last_error(state: dict) -> tuple[str, str] | None:
    """取最近一条工具失败事件：(detail 文本, 错误分类)。"""
    for e in reversed(state.get("tool_events") or []):
        if isinstance(e, dict) and e.get("status") == "error":
            cat = str(e.get("error_category") or "")
            return str(e.get("detail") or "")[:200], cat
    return None


def _wait_hint(text: str) -> str:
    m = re.search(r"约\s*(\d+)\s*分钟", text)
    return m.group(0) if m else "稍后"


def _outputs_lines(state: dict) -> list[str]:
    lines = []
    for o in (state.get("round_outputs") or []):
        label = (o.get("label") or "").strip() or o.get("type") or ""
        if label:
            lines.append(f"《{label}》（可下载）")
    return lines


def build_break_notice(state: dict) -> str:
    """强制收尾的两分类平台说明。数据全部来自平台状态（产出/失败分类/事件），零 LLM 调用。"""
    outputs_lines = _outputs_lines(state)
    err = _last_error(state)
    fail_streaks = state.get("fail_streaks") or {}
    no_progress = int(state.get("no_progress_streak") or 0)
    cats = [c for c in ("wait", "retryable", "dead_end") if int(fail_streaks.get(c) or 0) > 0]

    # ---- 原因与建议（按失败分类优先）----
    reason, advise = "", ""
    if err:
        detail = err[0]
        cat = err[1] or ("wait" if cats and cats[0] == "wait" else "")
        if cat == "wait":
            if "约" in detail:
                reason = f"外部通道正在冷却（{_wait_hint(detail)}恢复），不是链接或任务本身的问题"
                advise = "等待冷却结束后回复「继续」即可重试，或先告诉我调整方向。"
            else:
                reason = "外部通道被限流/冷却中（短时间内重复尝试结果相同）"
                advise = "等待恢复后回复「继续」重试，或切换其他可行方案。"
        elif cat == "dead_end":
            reason = "某条执行路径被系统拒绝（权限/不存在类），改用替代方案前不建议重复尝试"
            advise = "可以换一种方式继续（如换工具/换格式），或回复「继续」让我基于已有进展接着做。"
        elif cat == "retryable":
            reason = f"连续多次同类失败后停止盲试（最近错误：{detail[:60]}）"
            advise = "回复「继续」让我换一种方案重试，或直接说明你的想法。"
    if not reason:
        if cats and cats[0] == "wait":
            reason, advise = "外部通道冷却/风控中（等待型失败），短时间内重试结果相同", \
                             "等待冷却后回复「继续」重试；或先告诉我调整方向。"
        elif cats and cats[0] == "dead_end":
            reason, advise = "某条路径不可行（权限/不存在类）", \
                             "换一种方式继续（换工具/换方案），或回复「继续」基于已有进展接着做。"
        elif no_progress >= 5:
            reason, advise = "连续多轮未能取得有效进展，提前收尾", \
                             "回复「继续」让我重新评估并继续；或说明调整方向。"
        else:
            reason, advise = "连续多次尝试未取得进展", "回复「继续」从断点接着做，或告诉我怎么调整。"

    # ---- 两分类正文 ----
    if outputs_lines:
        body = (
            f"{PLATFORM_TAG}任务提前收尾。\n\n"
            f"**已完成**：{ '；'.join(outputs_lines) }\n\n"
            f"**原因**：{reason}。\n\n"
            f"**建议**：{advise}"
        )
    else:
        body = (
            f"{PLATFORM_TAG}任务未完成。\n\n"
            f"**原因**：{reason}。\n\n"
            f"**建议**：{advise}"
        )
    return body


def is_platform_notice(content: str) -> bool:
    return content.startswith(PLATFORM_TAG)


def mark_interrupted(session_id: str, round_id: int, reason: str, outputs_n: int,
                     asked_n: int = 0) -> None:
    """写续跑标记（Redis interrupted:{session}，3600s）——下轮 _load_history 注入续跑引导。

    兼容旧格式（纯 round_id 字符串）：新写成 JSON。写失败仅记日志不阻塞。
    asked_n：中断前已反问用户次数（恢复注记提示"别再问"）。
    """
    import asyncio

    from app.core.redis import redis_set
    from app.core.logging import get_logger

    try:
        # 2026-09-10（全仓缓存核查）：原直接调异步 redis_set 既没 await 也没建任务——协程被丢弃，
        # 该路径的 interrupted 标记**从来没有写进去过**（下轮不会注入续跑引导）。
        # 本函数是同步函数（调用方多为同步上下文），按项目惯例建任务派发。
        asyncio.get_event_loop().create_task(redis_set(
            f"interrupted:{session_id}", json.dumps(
                {"round": round_id, "reason": reason[:200], "outputs": outputs_n,
                 "asked": int(asked_n or 0)}, ensure_ascii=False), 3600))
    except Exception as e:
        get_logger("agent.break_notice").warning("中断标记写入失败: %s", str(e)[:100])
