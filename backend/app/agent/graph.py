"""LangGraph 状态图：多轮工具调用的 React 循环（含最终答复自校验）。

```
START → input_filter ─blocked→ END
       → plan_router → skill_router → agent_llm ─计划反问（【计划】标记）→ END（文字计划，等用户下条消息确认）
                          │ 有tool_calls → tool_exec（v2 框架：工具直接执行，无授权卡）──┐
                          │ 无tool_calls                                                 │ count<MAX
                          ▼                                                              ▼
                       verify ─有tool_calls──────────────────────────────→ agent_llm（强化循环）
                          │ 无tool_calls（校验通过，发 text）
                          ▼
                        END(落库,done)
       tool_exec 轮次超限/连败 → chat → END
```
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.agent_llm import run_agent_llm
from app.agent.nodes.chat import run_chat
from app.agent.nodes.input_filter import run_input_filter
from app.agent.nodes.plan_approval import run_plan_approval
from app.agent.nodes.plan_router import run_plan_router
from app.agent.nodes.skill_router import run_skill_router
from app.agent.nodes.tool_exec import run_tool_exec
from app.agent.nodes.verify import run_verify
from app.agent.state import AgentState


def _route_after_filter(state: AgentState) -> str:
    result = state.get("input_filter_result") or {}
    return "ok" if result.get("passed", True) else "blocked"


def _route_plan_router(state: AgentState) -> str:
    """v2：plan_router 输出 next 键（常规/批准门/收尾；反问由 LLM 经 ask_user 工具自主发起，不走路由）。"""
    nxt = state.get("next") or "skill_router"
    return nxt if nxt in ("skill_router", "plan_approval", "chat") else "skill_router"


def _route_after_approval(state: AgentState) -> str:
    """v2：计划批准卡结果路由——approved 直进执行；rejected 回路由器修订；timeout 收尾。"""
    pa = state.get("plan_approval") or ""
    if pa == "approved":
        return "skill_router"
    if pa == "rejected":
        return "plan_router"
    return "chat"


def _route_after_llm(state: AgentState) -> str:
    messages = state.get("messages") or []
    # 计划反问轮：计划已文字输出给用户，本轮结束等用户下一条消息确认
    # （绕过 verify——verify 会再调 LLM 生成最终答复，导致计划重复）
    if state.get("plan"):
        return "end"
    # 2026-09-11：插话暂停轮（interrupt_paused → END）已下线——用户要的是**当场执行**插话要求，
    # 暂停回"请回复继续"被视为 bug（16:37 部署机走查）。插话现在直接进上下文并由模型执行。
    # v2 框架：工具由 agent 直接执行（授权卡已下线；写门禁在 tool_exec 按 plan_status 确定性拒绝）
    if messages and messages[-1].get("tool_calls"):
        return "tool_exec"
    return "verify"


def _route_after_verify(state: AgentState) -> str:
    messages = state.get("messages") or []
    if messages and messages[-1].get("tool_calls"):
        # v2 框架：校验强化轮工具同样直接执行
        return "tool_exec"
    # 2026-09-11（用户场景：总结期间插话没人理）：verify 收尾前若检测到未消费的插话 →
    # 回 agent_llm 消费（它会把插话注入并要求当场执行），处理完再进入下一轮 verify 收尾。
    if state.get("interrupts_pending"):
        return "agent_llm"
    return "end"


def _route_after_tool(state: AgentState) -> str:
    # 2026-08-20（P1-①）：工具轮末探测到中断队列非空 → 优先回 agent_llm 消费——
    # 复用既有 B10 暂停机制（agent_llm.py：interrupted=True → 移除 tool_calls → 文字确认
    # → verify → END）。置于所有收尾分支之前：cap/force_final 轮次中到达的插话必被当轮
    # 消费（原实现插话滞留 Redis 至下个 ask——用户"插话消失、工具调用结束无收到"根因）。
    # cap 语义不被绕过：暂停轮不调工具，队列被 lpop_all 全量清空，不会多轮循环。
    if state.get("interrupts_pending"):
        return "agent_llm"
    if state.get("force_final"):
        return "chat"
    # 无进展检测（2026-08-07 设计；2026-08-14 放宽 4→5）：连续 5 轮工具全部无有效输出 → 终止（防死循环）。
    # 正常迭代/沙盒调试（每轮有 stdout/产出）不受此限。
    if (state.get("no_progress_streak") or 0) >= 5:
        return "chat"
    # B2（D7）：max_tool_rounds 硬兜底（防失控；v2 无 checkpoint 卡，到 cap 即收尾）
    cap = max(state.get("max_tool_rounds") or 20, 20)
    if (state.get("tool_round_count") or 0) >= cap:
        return "chat"
    # v2：complex 每轮工具后回 plan_router 复评 phase（P2→P3 观察推进/写门禁/修订回退）；
    # quick 保持原 React 循环直回 agent_llm
    if state.get("mode") == "complex":
        return "plan_router"
    return "agent_llm"


def build_graph() -> object:
    g = StateGraph(AgentState)
    g.add_node("input_filter", run_input_filter)
    # v2：模式/phase 路由器（反问由 LLM 经 ask_user 工具自主发起——不焊死；复杂 P0-P7
    # 状态机含批准门 plan_approval；批完回路由器继续）
    g.add_node("plan_router", run_plan_router)
    g.add_node("plan_approval", run_plan_approval)
    g.add_node("skill_router", run_skill_router)
    g.add_node("agent_llm", run_agent_llm)
    g.add_node("verify", run_verify)
    g.add_node("tool_exec", run_tool_exec)
    g.add_node("chat", run_chat)

    g.add_edge(START, "input_filter")
    # 踩坑 18：改条件边必须同步映射表
    g.add_conditional_edges("input_filter", _route_after_filter, {"ok": "plan_router", "blocked": END})
    g.add_conditional_edges("plan_router", _route_plan_router,
                            {"skill_router": "skill_router", "plan_approval": "plan_approval", "chat": "chat"})
    g.add_conditional_edges("plan_approval", _route_after_approval,
                            {"skill_router": "skill_router", "plan_router": "plan_router", "chat": "chat"})
    g.add_edge("skill_router", "agent_llm")
    g.add_conditional_edges("agent_llm", _route_after_llm, {"tool_exec": "tool_exec", "verify": "verify", "end": END})
    g.add_conditional_edges("verify", _route_after_verify,
                            {"tool_exec": "tool_exec", "agent_llm": "agent_llm", "end": END})
    g.add_conditional_edges("tool_exec", _route_after_tool,
                            {"agent_llm": "agent_llm", "chat": "chat", "plan_router": "plan_router"})
    g.add_edge("chat", END)
    return g.compile()
