"""M3 端到端验收脚本：SSE 流 + 工具直执行（v2 框架无授权卡）+ 图表产出。

断言：全程无 confirm/checkpoint 事件；intent/result 事件 ≥1 对（事件由 agent 经
intent_event/result_event 工具发出，批次 2 起生效——批次 1 期间计数为 0 不判 FAIL）；
chart + done 正常。

用法: conda run -n aip python tests/e2e_ask.py
"""
import asyncio
import json
import sys
from pathlib import Path

import re

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://localhost:8001/api/v1"


def _tool_event_english(d: dict) -> str | None:
    """工具调用**过程/结束**的展示文案不许出现英文（2026-09-24 用户走查要求）：
    工具名必须带中文 label；start 的"队列 X"必须是中文队列名（原来直接摆 read/default 这种 id）。"""
    if d.get("status") == "start":
        label = str(d.get("label") or "")
        if not any("\u4e00" <= ch <= "\u9fff" for ch in label):
            return f"tool={d.get('tool_name')} 缺中文 label"
        queue_part = str(d.get("detail") or "").split("，")[0]
        if re.search(r"[A-Za-z]{3,}", queue_part):
            return f"tool={d.get('tool_name')} 队列名疑似英文：{queue_part[:40]}"
    return None


async def ask(c: httpx.AsyncClient, headers: dict, payload: dict):
    chart_seen = done_seen = False
    tool_events = tool_violations = 0
    confirm_seen = checkpoint_seen = False
    intent_count = result_count = 0
    full_text = ""
    errors = []
    async with c.stream("POST", "/chat/ask", headers=headers, json=payload) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                if ev == "confirm":
                    confirm_seen = True
                    print(f"[CONFIRM] 意外收到授权卡事件: {json.dumps(d, ensure_ascii=False)[:160]}")
                elif ev == "checkpoint":
                    checkpoint_seen = True
                    print(f"[CHECKPOINT] 意外收到 checkpoint 事件: {json.dumps(d, ensure_ascii=False)[:160]}")
                elif ev == "intent":
                    intent_count += 1
                    print(f"[INTENT] {d.get('text', '')[:80]}")
                elif ev == "result":
                    result_count += 1
                    print(f"[RESULT] ok={d.get('ok')} {d.get('text', '')[:80]}")
                elif ev == "chart":
                    chart_seen = True
                    print(f"[CHART] {d['type']} | {d['label']} | option keys: {list(d['option'].keys())[:5]}")
                elif ev == "text":
                    full_text += d["delta"]
                elif ev == "done":
                    done_seen = True
                    print(f"[DONE] outputs: {[o['type'] for o in d['outputs']]}")
                elif ev == "tool":
                    tool_events += 1
                    bad = _tool_event_english(d)
                    if bad:
                        tool_violations += 1
                        print(f"[工具事件英语残留] {bad}")
                elif ev == "error":
                    errors.append(d)
                    print(f"[ERROR] {d}")
    print(f"RESULT: confirm={confirm_seen} checkpoint={checkpoint_seen} "
          f"intent={intent_count} result={result_count} chart={chart_seen} done={done_seen} errors={len(errors)} "
          f"tool_events={tool_events} 英语残留={tool_violations}")
    print(f"FINAL TEXT (first 120): {full_text[:120]!r}")
    return (confirm_seen, checkpoint_seen, intent_count, result_count, chart_seen, done_seen, errors,
            full_text, tool_events, tool_violations)


async def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else "把三个要点画成柱状图（数值自拟）"
    async with httpx.AsyncClient(base_url=BASE, timeout=400) as c:
        r = await c.post("/auth/login", json={"department_id": "demo", "username": "demo", "password": pw("demo")})
        token = r.cookies["access_token"]
        headers = {"Authorization": f"Bearer {token}", "X-Client-ID": "e2e-client"}
        print(f"=== ask: {question} ===")
        (confirm_seen, checkpoint_seen, intent_count, result_count, chart_seen, done_seen, errors, _,
         tool_events, tool_violations) = await ask(
            c, headers, {"question": question, "active_skills": ["file_parse", "generate_chart"]}
        )
        # v2 框架：授权卡/checkpoint 卡已移除，全程不得出现；工具直接执行应产出图表；
        # 2026-09-01（用户决策）：intent/result 事件不强修——改观察项（打印计数不 FAIL）；
        # chart 断言按档位自适应：agnes/glm 免费档工具遵循弱 → 软观察（只打印不 FAIL）；
        # deepseek 档 → chart 硬性断言（回归基线）；MOCK=1 → 模拟序列无图表工具，软观察
        import os

        mock_mode = os.environ.get("MOCK") == "1"
        from app.services.config_service import get_model_config

        llm_cfg = await get_model_config("demo", "employee", "llm") or {}
        is_free_tier = llm_cfg.get("platform") in ("agnes", "glm")
        print(f"[事件观察] intent={intent_count} result={result_count}（非 FAIL 条件）")
        if is_free_tier:
            print(f"[免费档观察] platform={llm_cfg.get('platform')} chart={chart_seen}（免费档软性）")
        if mock_mode:
            print(f"[mock 观察] chart={chart_seen}（模拟序列无图表工具）")
        # 2026-09-24：工具过程/结束文案不许出现英文（label 必须中文、队列名必须中文）
        ok = (not confirm_seen and not checkpoint_seen
              and (chart_seen or mock_mode or is_free_tier) and done_seen and not errors
              and not tool_violations)
        print("=== PASS ===" if ok else "=== FAIL ===")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
