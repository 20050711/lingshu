"""链路诊断：直接跑 LangGraph 全流程（mock confirm 立即通过），打点耗时。

用法: python tests/diag_chain.py "问题"
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.graph import build_graph
from app.agent.llm_client import DeepSeekLLM
from app.agent.tools import ToolContext
from app.services.confirm_waiter import ConfirmWaiter, get_confirm_waiter

# mock confirm：立即 approved，不等真实用户
async def fake_wait(self, session_id: str, round_id: int):
    return "approved", "cf_diag"

ConfirmWaiter.wait = fake_wait

# 打点节点耗时
import app.agent.nodes.agent_llm as agent_llm_mod
import app.agent.nodes.tool_exec as tool_exec_mod

_t0 = time.time()
_last = {"t": _t0, "count": 0}


def _mark(name: str):
    _last["count"] += 1
    now = time.time()
    print(f"[{now - _t0:6.1f}s] {name} (第{_last['count']}次)", flush=True)
    _last["t"] = now


async def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else "把三个要点画成柱状图（数值自拟）"
    orig_agent = agent_llm_mod.run_agent_llm
    orig_tool = tool_exec_mod.run_tool_exec

    async def spy_agent(state, config):
        _mark("agent_llm")
        result = await orig_agent(state, config)
        calls = [c["function"]["name"] for c in (state.get("pending_tool_calls") or [])]
        if calls:
            print(f"  → 本轮工具: {calls}", flush=True)
        return result

    async def spy_tool(state, config):
        _mark("tool_exec")
        result = await orig_tool(state, config)
        return result

    agent_llm_mod.run_agent_llm = spy_agent
    tool_exec_mod.run_tool_exec = spy_tool
    try:
        graph = build_graph()
        events = asyncio.Queue()
        state = {
            "session_id": "diag-sess", "round_id": 1,
            "user_question": question, "user_role": "employee",
            "department_id": "demo", "client_id": "diag",
            "active_skills": ["file_parse", "generate_chart"], "auto_skill": False, "multi_table": False,
            "messages": [], "tool_round_count": 0, "max_tool_rounds": 8,
            "force_final": False, "round_outputs": [], "confirm_status": "pending",
            "input_filter_result": {"passed": True},
        }
        cfg = {"configurable": {
            "events": events, "llm": DeepSeekLLM("employee"),
            "waiter": get_confirm_waiter(),
            "ctx": ToolContext("diag-sess", 1, "demo", "employee", "diag", "/tmp/diag_out"),
        }}
        final = await graph.ainvoke(state, cfg)
        print(f"[{time.time() - _t0:6.1f}s] DONE total_seconds={time.time() - _t0:.1f}", flush=True)
        outputs = final.get("round_outputs") or []
        print("outputs:", json.dumps([{k: v for k, v in o.items() if k != "option"} for o in outputs], ensure_ascii=False)[:300], flush=True)
        msgs = final.get("messages") or []
        last_text = [m.get("content", "") for m in msgs if m.get("role") == "assistant" and m.get("content")]
        print("final text:", repr(last_text[-1][:150]) if last_text else "(none)", flush=True)
        return 0 if outputs else 1
    finally:
        agent_llm_mod.run_agent_llm = orig_agent
        tool_exec_mod.run_tool_exec = orig_tool


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
