"""plan_approval 节点（v2 框架，2026-08-14）：计划批准卡——全流程唯一人工门。

广播 plan 事件（status=pending）→ await PlanWaiter.wait（900s 超时）：
- approved → plan 事件(status=confirmed) + phase=execute（P6 开始）
- rejected+意见 → plan_revision_count+1，回 plan_router（修订上限 3 次在那里判）
- timeout → force_final 收尾（plan_json 已持久化，用户下条消息按修订路径重开）
"""
from __future__ import annotations

import asyncio

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.logging import get_logger
from app.services.interaction_waiter import PlanWaiter

logger = get_logger("agent.plan_approval")


async def run_plan_approval(state: AgentState, config: RunnableConfig) -> dict:
    c = config["configurable"]
    events: asyncio.Queue = c["events"]
    waiter: PlanWaiter = c["plan_waiter"]

    plan_json = state.get("plan_json") or {}
    session_id = state.get("session_id", "")
    revision = state.get("plan_revision_count") or 0
    plan_id = waiter.new_plan_id()
    plan_json["plan_id"] = plan_id  # 落库与 deferred 批准路径匹配用

    await events.put({
        "event": "plan",
        "plan_id": plan_id,
        "goal": plan_json.get("goal", ""),
        "steps": plan_json.get("steps", []),
        "risks": plan_json.get("risks", []),
        "revision": revision + 1,
        "status": "pending",
        "expires_at": waiter.expires_at(),
    })
    logger.info("计划批准卡 session=%s plan_id=%s revision=%d steps=%d",
                str(session_id)[:8], plan_id, revision + 1, len(plan_json.get("steps", [])))

    decision, feedback = await waiter.wait(session_id, plan_id)

    if decision == "approved":
        await events.put({"event": "plan", "plan_id": plan_id, "goal": plan_json.get("goal", ""),
                          "steps": plan_json.get("steps", []), "risks": plan_json.get("risks", []),
                          "revision": revision + 1, "status": "confirmed"})
        logger.info("计划已批准 session=%s plan_id=%s", str(session_id)[:8], plan_id)
        return {
            "plan_approval": "approved",
            "plan_confirmed": True,
            "phase": "execute",
            "plan_json": plan_json,
        }
    if decision == "rejected":
        logger.info("计划被拒绝 session=%s 意见=%s", str(session_id)[:8], feedback[:80])
        return {
            "plan_approval": "rejected",
            "plan_revision_count": revision + 1,
            "plan_json": plan_json,
            # 用户意见经收敛注记注入下一轮 LLM（修订依据）
            "converge_note": f"【计划修订意见】用户不同意当前计划：{feedback.strip()[:500]}。请按意见修订计划并重新提交。",
        }
    # timeout：收尾（卡片保留语义由 plan_json 持久化承担）
    logger.info("计划批准超时 session=%s plan_id=%s", str(session_id)[:8], plan_id)
    return {
        "plan_approval": "timeout",
        "plan_json": plan_json,
        "force_final": True,
        "converge_note": "【收尾提示】计划批准超时。向用户说明：计划已保存，发送任意消息可重新打开计划批准卡。",
    }
