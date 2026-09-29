"""调试：观察 confirm 节点收到的 pending_tool_calls。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.graph import build_graph
from app.agent.llm_client import DeepSeekLLM
from app.agent.tools import ToolContext
from app.services.confirm_waiter import get_confirm_waiter

import app.agent.nodes.confirm as cf


async def main() -> None:
    orig_confirm = cf.run_confirm

    async def spy_confirm(state, config):
        print("CONFIRM sees pending_tool_calls:", json.dumps(state.get("pending_tool_calls"), ensure_ascii=False)[:300])
        print("CONFIRM sees last msg keys:", list((state.get("messages") or [])[-1].keys()))
        return {"confirm_status": "approved"}

    cf.run_confirm = spy_confirm
    try:
        graph = build_graph()
        events = asyncio.Queue()
        state = {
            "session_id": "dbg1", "round_id": 1,
            "user_question": "查看团队数据库的表结构", "user_role": "employee",
            "department_id": "demo", "client_id": "c1",
            "active_skills": ["file_parse", "generate_chart"], "auto_skill": False, "multi_table": False,
            "messages": [], "tool_round_count": 0, "max_tool_rounds": 8,
            "force_final": False, "round_outputs": [], "confirm_status": "pending",
            "input_filter_result": {"passed": True},
        }
        cfg = {"configurable": {
            "events": events, "llm": DeepSeekLLM("employee"),
            "waiter": get_confirm_waiter(),
            "ctx": ToolContext("s", "r", "demo", "employee", "c", "/tmp/o"),
        }}
        await graph.ainvoke(state, cfg)
        print("graph done")
    finally:
        cf.run_confirm = orig_confirm


if __name__ == "__main__":
    asyncio.run(main())
