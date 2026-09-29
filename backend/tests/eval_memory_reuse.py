"""Q10 智能助手记忆/脚本复用评测（2026-08-26，对照 docs/循环检查方向.md）。

三维度（2026-09-17 改为文件场景：团队库查询线已下线，数据源改用会话上传的 xlsx）：
A. 上轮产出记忆：轮 1 生成图表/汇总 → 轮 2 引用上轮产出 → 是否 read_output 复用而非重做
B. 脚本单行修改：轮 1 生成脚本 → 轮 2 改参数重跑 → 是否复用旧脚本（read_output + run_script edit/run）
C. 复用率：连续 3 轮相似提问 → 统计 file_parse 重复解析次数 / 同名图表重复生成次数

观察通道：chat_messages.tool_events（assistant 落库的工具调用序列）+ outputs。

模式：
- 默认 mock（后端 LLM_MOCK=1）：链路级验证（注入链路/落库完整性），不测 LLM 行为
- --live 真实调用（当前 model_layer 主 LLM；省钱建议先在模型页切 GLM flash 免费档）

用法: conda run -n aip python tests/eval_memory_reuse.py [--live]
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

from app.core.database import get_global_engine  # noqa: E402

BASE = "http://localhost:8001/api/v1"


async def ask(c: httpx.AsyncClient, headers: dict, session_id: str, question: str,
              skills: list[str], live: bool = False) -> tuple[str, list]:
    """发起一轮 ask，返回 (完整文本, SSE 事件类型序列)。live=True 用 GLM flash 免费档真实调用。"""
    events_seen: list[str] = []
    full_text = ""
    payload = {"question": question, "session_id": session_id,
               "active_skills": skills, "mode": "quick", "thinking": "off"}
    if live:
        # 2026-08-26：行为评测走 GLM 免费档（用户约定：DeepSeek 测试一律 mock，GLM 免费可真实调用）
        payload["aux_overrides"] = {"_main": {"platform": "glm", "model": "glm-4.7-flash"}}
    async with c.stream("POST", "/chat/ask", headers=headers, json=payload) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                events_seen.append(ev)
                d = json.loads(line[6:])
                if ev == "text":
                    full_text += d["delta"]
                elif ev == "error":
                    print(f"  [ERROR] {d}")
    return full_text, events_seen


async def load_round_tools(session_id: str) -> list[dict]:
    """按轮查 chat_messages：assistant 的 tool_events + outputs（观测工具调用序列）。"""
    engine = get_global_engine()
    out = []
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT round_id, role, outputs, tool_events FROM chat_messages "
                     "WHERE session_id=:s AND role='assistant' ORDER BY round_id"),
                {"s": session_id},
            )
        ).all()
        for r in rows:
            events = []
            for t in (r.tool_events or []):
                if not isinstance(t, dict) or "tool_name" not in t:
                    continue  # 事件项（kind:intent/result）非工具调用，跳过
                events.append({
                    "tool": t.get("tool_name"),
                    "status": t.get("status"),
                    "brief": (t.get("brief") or "")[:100],
                })
            out.append({"round": r.round_id, "outputs": r.outputs or [], "tools": events})
    return out


def count_tool(rounds: list[dict], name: str, brief_contains: str = "") -> int:
    return sum(1 for r in rounds for t in r["tools"]
               if t["tool"] == name and (brief_contains in (t["brief"] or "")))


def summarize(rounds: list[dict]) -> None:
    for r in rounds:
        seq = " → ".join(t["tool"] for t in r["tools"]) or "（无工具）"
        outs = "，".join(f"{o.get('type')}:{o.get('label', '')[:20]}" for o in (r["outputs"] or [])) or "无产出"
        print(f"  轮{r['round']}: [{seq}] 产出[{outs}]")


async def run_scene(c: httpx.AsyncClient, headers: dict, session_id: str, skills: list[str],
                    rounds_q: list[str], label: str, live: bool = False,
                    file_ids: list[int] | None = None) -> list[dict]:
    print(f"\n=== 场景 {label} ===")
    base = len(await load_round_tools(session_id))  # 场景起始轮位（同 session 连续轮次，需切片）
    for i, q in enumerate(rounds_q, 1):
        print(f"  [轮{i}] {q}")
        await ask(c, headers, session_id, q, skills, live=live, file_ids=file_ids)
    all_rounds = await load_round_tools(session_id)
    rounds = all_rounds[base:]
    summarize(rounds)
    return rounds


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="真实调用主 LLM（默认 mock 链路验证）")
    args = parser.parse_args()
    if args.live:
        os.environ.pop("LLM_MOCK", None)
    else:
        os.environ["LLM_MOCK"] = "1"

    async with httpx.AsyncClient(base_url=BASE, timeout=400) as c:
        r = await c.post("/auth/login", json={"department_id": "demo", "username": "demo",
                                              "password": pw("demo")})
        token = r.cookies["access_token"]
        headers = {"Authorization": f"Bearer {token}", "X-Client-ID": "eval-memory"}
        sid = (await c.post("/chat/sessions", headers=headers, json={"client_id": "eval-memory"})).json()["id"]
        print(f"评测会话: {sid}（{'LIVE' if args.live else 'MOCK'}）")

        # 数据源夹具：会话上传一份 xlsx（原团队库查询场景已随下线移除）
        fixture = Path(__file__).resolve().parents[2] / "baogaoshengchengceshi" / "原始数据" / "渠道数据.xlsx"
        with open(fixture, "rb") as f:
            ur = await c.post("/chat/files", headers=headers, data={"session_id": sid},
                              files={"files": (fixture.name, f)})
        file_ids = [x["file_id"] for x in ur.json()["files"]]
        print(f"夹具已上传: {fixture.name} → file_ids={file_ids}")

        skills = ["file_parse", "generate_chart", "run_script", "read_output", "file_search"]
        report = {}

        # 场景 A：上轮产出记忆（轮 2 引用轮 1 产出）
        sA = await run_scene(c, headers, sid, skills, [
            "分析我上传的这份表格，按渠道汇总消费，并画柱状图",
            "上一轮生成的图表里，全站智投的消费是多少？直接用已有数据回答，不要重新生成图表",
        ], "A 上轮产出记忆", live=args.live, file_ids=file_ids)
        report["A"] = {
            "read_output_count": count_tool(sA, "read_output"),
            "file_parse_count": count_tool(sA, "file_parse"),
            "chart_count": count_tool(sA, "generate_chart"),
        }

        # 场景 B：脚本单行修改（轮 2 改轮 1 脚本参数）
        sB = await run_scene(c, headers, sid, skills, [
            "写一个 python 脚本读取我上传的表格，输出消费 Top3，脚本放沙盒，跑一遍输出结果",
            "把刚才脚本里的 Top3 改成 Top5 再跑一遍，输出前 5",
        ], "B 脚本单行修改复用", live=args.live, file_ids=file_ids)
        report["B"] = {
            "read_output_count": count_tool(sB, "read_output"),
            "run_script_count": count_tool(sB, "run_script"),
            "script_edit": count_tool(sB, "run_script", "edit"),
            "script_run": count_tool(sB, "run_script", "run"),
        }

        # 场景 C：相似查询复用率（3 轮同主题）
        sC = await run_scene(c, headers, sid, skills, [
            "我上传的这份表格，各时段渠道消费分布怎么样？",
            "再按小时时段看一下这份表格的渠道数据，哪个时段效果最好",
            "最后确认一下 0-23 点各时段渠道表现",
        ], "C 相似提问复用率", live=args.live, file_ids=file_ids)
        report["C"] = {
            "file_parse_calls": count_tool(sC, "file_parse"),
            "chart_calls": count_tool(sC, "generate_chart"),
            "rounds": len(sC),
        }

        print("\n=== 评测统计 ===")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
