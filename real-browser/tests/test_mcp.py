"""MCP 协议联通回归：起真服务端（stdio）→ 握手 → 列工具 → 走护栏拒绝路径。

跑法（Windows 侧）：E:\\stealth-browser-win\\.venv\\Scripts\\python.exe <repo>\\tests\\test_mcp.py
说明：用真实 MCP 客户端（mcp.client.stdio）走 stdio，不 mock。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

EXPECT_TOOLS = sorted([
    "browse_open", "browse_snapshot", "browse_click", "browse_type", "browse_scroll",
    "browse_press", "browse_back", "browse_text", "browse_screenshot", "browse_download",
    "browse_eval", "browse_extract",
    "browse_login_status", "browse_ask_human", "browse_resume", "browse_close",
])


async def run() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async with stdio_client(StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_browser.mcp_server", "--identity", "__mcp_test__"])) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            ok_tools = names == EXPECT_TOOLS
            print(f"{'PASS' if ok_tools else 'FAIL'}  工具面清单（{len(names)} 个）"
                  + ("" if ok_tools else f"\n  实际：{names}\n  期望：{EXPECT_TOOLS}"))

            # 护栏拒绝路径：内网地址必须被 URL 闸挡下，且经 MCP 全链路返回人话
            res = await session.call_tool("browse_open", {"url": "http://127.0.0.1:9/"})
            text = res.content[0].text if res.content else ""
            ok_guard = "被护栏拒绝" in text and "内网" in text
            print(f"{'PASS' if ok_guard else 'FAIL'}  护栏拒绝（SSRF 拦截经 MCP 全链路）"
                  + ("" if ok_guard else f"\n  实际：{text[:200]}"))

            # instructions 是否带给了 agent（这是"提示词包"的分发渠道之一）
            instr = getattr(session, "_init_result", None)
            print("NOTE  instructions 由服务端下发（客户端可读；此处不额外断言）")

    return 0 if (ok_tools and ok_guard) else 1


def _quiet(loop, context):
    """Windows 上 stdio 客户端收尾会报 "I/O operation on closed pipe"——与测试无关，静音。"""
    exc = context.get("exception")
    if exc and "closed pipe" in str(exc):
        return
    loop.default_exception_handler(context)


def _unraisable(unraisable):
    """CPython 在 Windows 上退出时会为"未关闭的 pipe transport"打一堆 Exception ignored——
    那是解释器自身的收尾噪音（__repr__ 里访问已关闭的 fd），与测试结果无关。"""
    if "closed pipe" in str(unraisable.exc_value):
        return
    sys.__unraisablehook__(unraisable)


if __name__ == "__main__":
    sys.unraisablehook = _unraisable
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(_quiet)
    try:
        rc = loop.run_until_complete(run())
    finally:
        try:
            loop.run_until_complete(asyncio.sleep(0))  # 放掉挂起的收尾回调
        except Exception:
            pass
        loop.close()
    sys.exit(rc)
