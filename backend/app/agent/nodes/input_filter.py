"""input_filter 节点：提示词注入/越权检测。

未通过 → 记录 security_events + 发 error 事件 → 条件边走 END。
"""
from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.input_filter import InputFilter


async def run_input_filter(state: AgentState, config: RunnableConfig) -> dict:
    events: asyncio.Queue = config["configurable"]["events"]
    question = state.get("user_question", "")
    # E-10(API)：CEO 放行跨团队规则（cross_dept）——看全局数据是其职责
    result = InputFilter.check(
        question, state.get("department_id", ""), state.get("allowed_tools"),
        role=state.get("user_role", "employee"),
    )

    if not result["passed"]:
        # 记录安全事件
        try:
            from sqlalchemy import insert, text

            from app.core.database import get_global_engine
            from app.models import SecurityEvent

            engine = get_global_engine()
            async with engine.begin() as conn:
                await conn.execute(
                    insert(SecurityEvent).values(
                        account_id=state.get("client_id", ""),
                        input_hash=InputFilter.hash_text(question),
                        matched_rule=result["rule"],
                        action="blocked",
                    )
                )
        except Exception:
            pass
        await events.put({"event": "error", "code": "E010", "message": result["reason"]})
        return {"input_filter_result": result, "error": result["reason"]}

    return {"input_filter_result": result}
