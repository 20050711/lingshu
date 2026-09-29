"""plan_router 节点：v2 双模式路由器（2026-08-14 起）+ D20 计划确认状态机（保留）。

v2 职责：ask 启动路由——
- quick：直接进工具循环（需求反问由 LLM 经 ask_user 工具自主发起——不焊死）
- complex：P0-P7 phase 状态机（explore/design/approval/execute/final）——phase 推进靠确定性
  观察（P2 探索汇合 / P3 计划 JSON 解析），批准门 plan_approval 为唯一人工门；
  需求澄清/方案取舍由 LLM 按提示词经 ask_user 自主反问（任务型需求"往死里反问"）
- 输出 {"next": "skill_router"|"plan_approval"|"chat"}，graph 条件边据此路由

D20 职责（保留，quick 模式文字计划遗留兼容）：
- sessions.plan_status 非 pending/confirmed → 放行
- confirmed 残留 → 视同已确认
- pending → 惰性过期检查 → LLM 分类用户消息 → 原子 UPDATE 确认/拒绝
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from langchain_core.runnables import RunnableConfig
from sqlalchemy import text

from app.agent.state import AgentState
from app.core.database import get_global_engine
from app.core.logging import get_logger

logger = get_logger("agent.plan_router")

_CLASSIFY_PROMPT = """你是计划确认判定器。以下是用户之前收到的执行计划，以及用户刚发来的最新一条消息。
判定用户意图，只输出一个词：
- confirm：用户明确同意/确认了该计划（如"可以/同意/就这么办/按计划执行/开始吧/好的"）
- modify：用户对该计划提出修改意见、疑问或补充要求
- reject：用户明确拒绝/放弃该计划（如"算了/不用了/别做了/不用执行"）
- other：与计划无关的新问题、闲聊、寒暄

计划：
{plan}

用户最新消息：
{message}

只输出一个词：confirm / modify / reject / other"""

# v2 复杂任务各阶段注入指令（P0-P7；反问不焊死——LLM 按需 ask_user）
# 2026-08-26：去阶段编号标记（P2/P3/P6），保留阶段名
PHASE_INSTRUCTIONS = {
    "explore": (
        "【当前阶段：需求澄清与探索调研】任务型需求动手前必须把关键点问清——"
        "缺目标对象/缺范围（时间、团队、数据口径）/多义口径/成功标准不明/存在影响方案走向的取舍时，"
        "先用 ask_user 向用户提问（一次把关键点问全——本阶段反问最多 1 轮，之后基于答案执行）。"
        "需求清楚后：并行派出 1-3 个 Explore 子代理（subagent 工具，role=explore、mode=background），"
        "随后用 subagent(mode=wait_all, ids=[...]) 汇合全部报告。只做只读调研与提问，不执行任何产出/写操作。"
    ),
    "design": (
        "【当前阶段：方案设计】派 1 个 Plan 子代理（subagent 工具，role=plan、mode=wait）"
        "产出计划 JSON（goal/steps[2-8 条，每步含 id、title、intent、verify]/risks/questions_answered）。"
        "把探索结论与澄清结果写进任务书。收到计划后不要自行修改；"
        "若计划存在影响用户利益的取舍点，用 ask_user 再问一轮用户意见（本轮反问总额度 2 次，已用完则做合理假设并在计划中标注）。"
    ),
    "execute": (
        "【当前阶段：执行】计划已批准，按计划步骤执行：每步开始调 todo_step(action=start, step_id=...)；"
        "完成调 todo_step(action=done, summary=结果一句话)；失败调 todo_step(action=fail, summary=原因)。"
        "若用户中途插话推翻了已批准计划，调 todo_step(action=replan) 回到计划批准。"
        "全部步骤完成后输出业务四板块汇报（本次完成/依据与来源/暂未覆盖/后续建议）。"
    ),
    "execute_approved": (
        "【计划已批准，恢复执行】按已批准计划继续：从尚未完成的步骤开始，"
        "每步 todo_step(start) → 执行 → todo_step(done/fail)。全部完成后输出业务四板块汇报"
        "（本次完成/依据与来源/暂未覆盖/后续建议）。"
    ),
}


def _revision_instruction(feedback: str) -> str:
    return (
        "【当前阶段：计划修订】用户不同意当前计划。请派 Plan 子代理"
        f"（subagent 工具，role=plan、mode=wait）按以下意见修订计划并重新提交（保留已完成步骤的 id）：\n{feedback}"
    )


async def _advance_phase(state: AgentState) -> dict:
    """确定性 phase 推进（观察 tool_exec 写入的标记，不靠 LLM 自报）。

    返回状态更新（可能含 phase/plan_json/plan_approval/phase_instruction/converge_note）。
    """
    phase = state.get("phase") or "explore"
    plan_approval = state.get("plan_approval") or ""

    # P2 探索完成（wait_all 汇合成功）→ design
    if phase == "explore" and state.get("explore_done"):
        logger.info("P2 探索完成 → design session=%s", str(state.get("session_id", ""))[:8])
        return {"phase": "design", "explore_done": False}

    # P3 计划候选解析（Plan 子代理报告）→ approval
    if phase in ("design", "approval") and state.get("plan_candidate"):
        from app.agent.plan_json import parse_plan_json

        plan_json, err = parse_plan_json(state["plan_candidate"])
        if plan_json:
            logger.info("P3 计划解析成功 → approval session=%s steps=%d",
                        str(state.get("session_id", ""))[:8], len(plan_json["steps"]))
            out: dict = {"plan_json": plan_json, "phase": "approval", "plan_candidate": None,
                         "phase_retries": 0}
            if plan_approval == "rejected":
                # 修订重提：清 rejected 标记（重新提交批准卡）
                out["plan_approval"] = ""
            return out
        retries = (state.get("phase_retries") or 0) + 1
        logger.warning("P3 计划解析失败 retry=%d err=%s", retries, err[:120])
        if retries > 2:
            return {"converge_note": "【卡点报告】计划 JSON 连续解析失败（已重试 2 次）。"
                                     "向用户如实报告卡点与原因，不要继续派子代理。",
                    "force_final": True, "phase_retries": retries, "plan_candidate": None}
        return {"phase_retries": retries, "plan_candidate": None,
                "phase_instruction": f"【计划解析失败】上一次 Plan 子代理输出无法解析（{err}）。"
                                     "请重新派一个**新的** Plan 子代理（subagent，role=plan、mode=wait，"
                                     "不要用 resume_id 续聊旧子代理），严格按 schema 只输出计划 JSON（≤8 千字）。",
                "phase": "design"}
    return {}


async def _complex_route(state: AgentState, config: RunnableConfig) -> dict:
    """complex P0-P7 状态机路由（批准门 + 阶段指令；反问由 LLM 自主发起）。"""
    phase = state.get("phase") or "explore"
    plan_status = state.get("plan_status") or "none"
    plan_json = state.get("plan_json")
    plan_approval = state.get("plan_approval") or ""
    revision = state.get("plan_revision_count") or 0

    # 跨 ask 恢复：已批准计划 → 直接执行（D20 语义复用，plan_confirmed 触发 persist 复位）
    if plan_status == "confirmed":
        return {"phase": "execute", "plan_confirmed": True, "next": "skill_router",
                "phase_instruction": PHASE_INSTRUCTIONS["execute_approved"]}
    # 跨 ask：批准卡超时/被拒后用户再发消息 → 用户消息即修订意见（走 design 修订循环重开批准卡）
    if plan_status == "pending" and plan_json and not plan_approval:
        feedback = state.get("user_question", "")
        logger.info("pending 计划重开（用户消息为修订意见）session=%s", str(state.get("session_id", ""))[:8])
        return {"phase": "design", "plan_approval": "rejected", "next": "skill_router",
                "phase_instruction": _revision_instruction(feedback[:500])}

    # 批准门（唯一人工门）
    if phase == "approval":
        if plan_json and plan_approval == "":
            return {"next": "plan_approval"}
        if plan_approval == "rejected":
            if revision < 3:
                return {"phase": "design", "next": "skill_router",
                        "phase_instruction": _revision_instruction(state.get("converge_note") or "")}
            # 修订循环上限 3 次 → 卡点收尾
            return {"next": "chat", "force_final": True,
                    "converge_note": "【卡点报告】计划修订已达上限（3 次仍不同意）。"
                                     "向用户说明卡点与最新计划要点，等待用户进一步指示。"}
        if plan_approval == "timeout":
            return {"next": "chat"}
        # 无计划 JSON 但处于 approval（异常路径）→ 回 design 重新设计
        return {"phase": "design", "next": "skill_router", "phase_instruction": PHASE_INSTRUCTIONS["design"]}

    # 探索/设计/执行/收尾阶段：注入阶段指令继续循环
    if phase in ("explore", "design", "execute", "final"):
        return {"next": "skill_router", "phase_instruction": PHASE_INSTRUCTIONS.get(phase, "")}
    return {"next": "skill_router"}


async def _d20_transition(state: AgentState, config: RunnableConfig) -> dict:
    """D20 计划状态机（quick 文字计划遗留兼容；返回状态更新或空 dict）。"""
    status = state.get("plan_status") or "none"
    if status not in ("pending", "confirmed"):
        return {}
    session_id = state.get("session_id", "")
    if status == "confirmed":
        # 并发残留（两个 ask 并发读历史）：视同已确认——执行轮 persist 会复位 none
        return {"plan_status": "confirmed", "plan_confirmed": True}

    # 惰性过期检查（无后台任务：ask 启动时判断；DB 时间与 state 均 naive，踩坑 4）
    expires_at = state.get("plan_expires_at")
    if expires_at:
        try:
            if datetime.now() > datetime.fromisoformat(expires_at):
                async with get_global_engine().begin() as conn:
                    await conn.execute(
                        text("UPDATE sessions SET plan_status='expired' "
                             "WHERE id=:s AND plan_status='pending'"),
                        {"s": session_id},
                    )
                return {"plan_status": "expired"}
        except (ValueError, TypeError):
            pass

    verdict = "other"
    try:
        msg = await asyncio.wait_for(
            config["configurable"]["llm"].ainvoke(
                [{"role": "user", "content": _CLASSIFY_PROMPT.format(
                    plan=(state.get("plan_text") or "")[:2000],
                    message=state.get("user_question", "")[:500])}],
                tools=None,
                stream_cb=None,
            ),
            timeout=60,
        )
        content = (msg.get("content") or "").strip().lower()
        if content in ("confirm", "modify", "reject", "other"):
            verdict = content
    except Exception as e:  # noqa: BLE001
        logger.warning("计划分类失败（按 other 处理）session=%s err=%s",
                       str(session_id)[:8], str(e)[:120])

    engine = get_global_engine()
    if verdict == "confirm":
        async with engine.begin() as conn:
            r = await conn.execute(
                text("UPDATE sessions SET plan_status='confirmed' "
                     "WHERE id=:s AND plan_status='pending'"),
                {"s": session_id},
            )
        if r.rowcount == 0:
            # 并发已被消费（另一 ask 已确认/拒绝）→ 重读实际值
            async with engine.connect() as conn:
                row = (await conn.execute(
                    text("SELECT plan_status FROM sessions WHERE id=:s"), {"s": session_id})).first()
            cur = row[0] if row else "none"
            if cur == "confirmed":
                return {"plan_status": "confirmed", "plan_confirmed": True}
            return {"plan_status": cur or "none"}
        logger.info("计划已确认 session=%s", str(session_id)[:8])
        return {"plan_status": "confirmed", "plan_confirmed": True}

    # modify / reject / other / 分类失败 → 一律 rejected（安全侧：宁可不执行，agent 重新出计划）
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET plan_status='rejected' "
                 "WHERE id=:s AND plan_status='pending'"),
            {"s": session_id},
        )
    logger.info("计划未获确认 session=%s verdict=%s", str(session_id)[:8], verdict)
    return {"plan_status": "rejected"}


async def run_plan_router(state: AgentState, config: RunnableConfig) -> dict:
    # v2 复杂任务：P0-P7 状态机（先做 phase 观察推进，再路由）
    # 踩坑：advances 必须合并进节点返回值——LangGraph 只认 return 的 dict，
    # 局部 merged 不落地则 phase 永不推进（explore_done/plan_candidate 永不被消费）
    if state.get("mode") == "complex":
        advances = await _advance_phase(state)
        if advances:
            # 若推进导致 force_final（解析超限）→ 直接收尾
            if advances.get("force_final"):
                advances["next"] = "chat"
                return advances
            merged = dict(state)
            merged.update(advances)
            route = await _complex_route(merged, config)
            return {**advances, **route}
        return await _complex_route(state, config)

    # D20 计划状态机（quick 模式遗留兼容；complex 不经过此路径）
    d20 = await _d20_transition(state, config)
    if d20:
        d20["next"] = "skill_router"
        return d20

    # quick：直接进工具循环（需求反问由 LLM 经 ask_user 工具自主发起——不焊死）
    return {"next": "skill_router"}
