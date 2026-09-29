"""支柱 1 单测（2026-08-10）：子代理机制——纯函数 + FakeLLM 循环 + 沙盒隔离。

用法: cd backend && source scripts/env_aip.sh && python -u tests/test_subagent.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


class FakeLLM:
    """脚本化响应序列：每次 ainvoke 弹出下一个响应（耗尽后返回默认报告）。"""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)

    async def ainvoke(self, messages, tools=None, stream_cb=None):
        if self.responses:
            return self.responses.pop(0)
        return {"role": "assistant", "content": "默认报告：已完成。", "tool_calls": None}


def _ctx(events: asyncio.Queue | None = None) -> "object":
    from app.agent.tools import ToolContext

    return ToolContext("smoke-sub", 1, "demo", "employee", "smoke",
                       "/data/outputs/smoke-sub/1", user_id=1, events=events)


async def main() -> int:
    from app.agent.subagent import ROLE_TOOLS, _effective_result, _resolve_subagent_tools, _result_to_text, run_subagent
    from app.agent.tools import get_tool
    from app.agent.tools.script_sandbox import _work_dir

    # 1. 纯函数：白名单解析（v2 角色化签名）/ 有效判定 / 结果回填
    # 2026-09-17：sql_query 下线（子代理工具面同步删除），越权过滤改用 file_parse 验证
    check("generic 白名单过滤", _resolve_subagent_tools("generic", ["run_script", "doc_export", "file_parse"]) == ["file_parse", "run_script"],
          str(_resolve_subagent_tools("generic", ["run_script", "doc_export", "file_parse"])))
    check("越权全拒", _resolve_subagent_tools("generic", ["doc_export", "memory"]) == [], "")
    defa = _resolve_subagent_tools("generic", None)
    check("generic 默认全子集", "run_script" in defa and "doc_export" not in defa and len(defa) >= 6, str(defa))
    _e_tools = _resolve_subagent_tools("explore", None)
    check("explore 纯只读（无 run_script；无写工具）",
          "run_script" not in _e_tools and "doc_export" not in _e_tools and "file_parse" in _e_tools, "")
    check("execute 含交付工具", "doc_export" in _resolve_subagent_tools("execute", None)
          and "run_script" in _resolve_subagent_tools("execute", None), "")
    check("plan 无工具", _resolve_subagent_tools("plan", None) == [], "")
    check("角色工具面常量完整", set(ROLE_TOOLS) == {"generic", "explore", "plan", "execute"}, str(ROLE_TOOLS))
    check("有效判定", _effective_result({"stdout": "x"}) and not _effective_result({"error": "e"}) and not _effective_result(None), "")
    # 2026-09-22（走查）：判定改为按工具 progress_keys——原先看硬编码全局键列表，
    # file_search / customer_list·customer_tree / file_parse(xlsx) 的返回键全不在列表里，成功结果
    # 被判"无有效输出"，两个调用即熔断（子代理第 1 轮被强杀）。
    check("file_search 结果算有效", _effective_result({"content_matches": [{"x": 1}]}, "file_search"), "")
    check("file_search 空结果算无效", not _effective_result({"content_matches": [], "file_matches": []}, "file_search"), "")
    check("客户清单算有效", _effective_result({"customers": [{"id": 1}]}, "customer_list"), "")
    check("客户目录树算有效", _effective_result({"folders": [], "files": [{"f": 1}]}, "customer_tree"), "")
    check("xlsx 解析算有效", _effective_result({"sheets": [{"name": "S1"}], "sheet_summary": "1 sheet"}, "file_parse"), "")
    check("partial 结果算有效", _effective_result({"error": "部分失败", "partial": True, "file_matches": [{"a": 1}]}, "file_search"), "")
    big = _result_to_text({"stdout": "x" * 10000, "exit_code": 0}, 400)
    check("内部护栏截断", len(big) <= 400 and json.loads(big)["exit_code"] == 0, f"len={len(big)}")

    # 2. 入口断言：events/llm 缺失 → error（skill_internal 路径天然免疫）；mode/role 非法校验
    r = await run_subagent({"task": "t"}, _ctx())
    check("入口断言", isinstance(r, dict) and "events/llm" in r.get("error", ""), str(r.get("error"))[:60])
    q0 = asyncio.Queue()
    ctx0 = _ctx(q0)
    ctx0.llm = FakeLLM([])
    check("非法角色拒绝", "未知子代理角色" in (await run_subagent({"task": "t", "role": "hacker"}, ctx0)).get("error", ""), "")
    check("非法 mode 拒绝", "未知子代理 mode" in (await run_subagent({"task": "t", "mode": "nope"}, ctx0)).get("error", ""), "")

    # 3. 正常路径：FakeLLM 直接出报告；事件全部带 subagent_id
    q = asyncio.Queue()
    ctx = _ctx(q)
    ctx.llm = FakeLLM([{"role": "assistant", "content": "报告：解析完成", "tool_calls": None}])
    r = await run_subagent({"task": "解析模板"}, ctx)
    check("正常出报告", r.get("report") == "报告：解析完成" and r.get("rounds_used") == 1 and not r.get("error"), str(r)[:80])
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    check("事件带 subagent_id", events and all(e.get("subagent_id") == "s-1" for e in events), str(events)[:80])

    # 4. 越权工具持续调用 → failures≥2 → 强制收尾（不调真实工具，dispatch 前被白名单拒绝）
    q2 = asyncio.Queue()
    ctx2 = _ctx(q2)
    bad = {"role": "assistant", "content": "", "reasoning_content": "r",
           "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "doc_export", "arguments": "{}"}}]}
    ctx2.llm = FakeLLM([bad, bad, {"role": "assistant", "content": "收尾报告", "tool_calls": None}])
    r2 = await run_subagent({"task": "任务"}, ctx2)
    check("越权强制收尾", r2.get("error") and "强制" in str(r2.get("error")), str(r2.get("error"))[:60])
    check("收尾有报告", bool(r2.get("report")), "")

    # 5. 沙盒隔离：子代理 ctx 下 write 可写、deliver 禁用、work 子目录生效
    t = get_tool("run_script")
    wctx = _ctx()
    wctx.subagent_id = "s-1"
    wctx.work_subdir = "sub_1"
    rw = await t.handler({"mode": "write", "file": "x.txt", "code": "hi"}, wctx)
    check("子代理 write 正常", isinstance(rw, dict) and "已写入" in rw.get("stdout", ""), str(rw)[:60])
    rd = await t.handler({"mode": "deliver", "file": "x.txt"}, wctx)
    check("子代理 deliver 禁用", isinstance(rd, dict) and "不允许 deliver" in rd.get("error", ""), str(rd.get("error"))[:60])
    wd = _work_dir(wctx)
    check("work 子目录", str(wd).endswith("work/sub_1") and wd.parent.name == "work", str(wd))

    # 6. v2：后台化 + 续聊（registry：background → wait_all 汇合 → resume 续聊 → evict）
    from app.agent.subagent_registry import SubagentTask, get_subagent_registry

    registry = get_subagent_registry()
    registry.ttl = 2  # 测试用短 TTL（evict 验证）
    q3 = asyncio.Queue()
    ctx3 = _ctx(q3)
    ctx3.llm = FakeLLM([{"role": "assistant", "content": "后台报告A", "tool_calls": None}])
    rb = await run_subagent({"task": "后台任务", "mode": "background"}, ctx3)
    check("background 立即返回 running", rb.get("status") == "running" and rb.get("subagent_id", "").startswith("s-"),
          str(rb)[:80])
    bg_id = rb["subagent_id"]
    await asyncio.sleep(0.2)  # 后台任务完成
    rep = await run_subagent({"mode": "wait_all", "ids": [bg_id]}, ctx3)
    reports = rep.get("reports") or []
    check("wait_all 汇合 done 报告", len(reports) == 1 and reports[0]["status"] == "done"
          and reports[0]["report"] == "后台报告A", str(reports)[:120])
    # 未知 id → 重派提示（重启语义）
    rep2 = await run_subagent({"mode": "poll", "ids": ["s-9999"]}, ctx3)
    check("未知 id 重派提示", "重新派发" in (rep2.get("reports") or [{}])[0].get("error", ""), str(rep2)[:100])
    # resume 续聊（保留上下文）：任务完成后的续聊走 wait 同步路径
    ctx3.subagent_counter = 9
    ctx3.llm = FakeLLM([{"role": "assistant", "content": "续聊报告B", "tool_calls": None}])
    rr = await run_subagent({"task": "续聊任务", "resume_id": bg_id}, ctx3)
    check("resume 续聊保留上下文", rr.get("report") == "续聊报告B" and rr.get("subagent_id") == bg_id, str(rr)[:100])
    # evict：短 TTL 惰性清理
    rec = registry.get(bg_id)
    rec.finished_at -= 10
    n = registry.evict_expired()
    check("TTL evict", n >= 1 and registry.get(bg_id) is None, f"evicted={n}")

    print(f"=== {'PASS' if FAIL == 0 else 'FAIL'}（{PASS} 过 / {FAIL} 败）===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
