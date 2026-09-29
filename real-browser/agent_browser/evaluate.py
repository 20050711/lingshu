"""开放读取通道：让 agent 自己写 JS 读页面——**跑在隔离世界**。

为什么是隔离世界（2026-09-24 定）：
- 页面**看不见**隔离世界里的执行——rebrowser 检测页自己的说明："如果测试没被触发，
  说明你在隔离世界执行，这是安全的、不可检测的"（`mainWorldExecution` 那一条）。
- 主世界 `Runtime.evaluate` 会被同类测试抓到，所以**默认不提供主世界执行**；
  确需调用页面自身函数时才显式 `world="main"`（档案里可禁）。

边界（写进 MCP instructions 教给 agent）：
- 这个通道**只用来读**（取值、找元素、抽结构化数据）；**任何"点/输入"都不许用它做**——
  JS 合成事件 `isTrusted=false`，行为采集器一眼看穿。
"""
from __future__ import annotations

import json
from typing import Any

DEFAULT_TIMEOUT_MS = 5000
MAX_RESULT_CHARS = 20000


class EvalError(Exception):
    """脚本执行失败（消息面向 agent 可读）。"""


async def eval_isolated(page, expression: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Any:
    """在隔离世界求值；返回值必须可序列化（对象用 JSON.stringify 包一层更稳）。"""
    cdp = await page.context.new_cdp_session(page)
    try:
        tree = await cdp.send("Page.getFrameTree")
        frame_id = tree["frameTree"]["frame"]["id"]
        world = await cdp.send("Page.createIsolatedWorld", {
            "frameId": frame_id, "worldName": "ab_read", "grantUniveralAccess": False})
        ctx_id = world["executionContextId"]
        # 注意：Playwright 的 cdp.send 返回的已经是 result 载荷本身（不再往外包一层 "result"）
        res = await cdp.send("Runtime.evaluate", {
            "expression": expression,
            "contextId": ctx_id,
            "returnByValue": True,
            "awaitPromise": True,
            "timeout": timeout_ms,
        })
        if "exceptionDetails" in res:
            detail = res["exceptionDetails"]
            msg = (detail.get("exception") or {}).get("description") or detail.get("text") or "未知错误"
            raise EvalError(f"脚本报错：{str(msg)[:300]}")
        return (res.get("result") or {}).get("value")
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


async def eval_main(page, expression: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Any:
    """主世界求值（**会被页面侧的 mainWorldExecution 类测试看见**，仅在必要时用）。"""
    try:
        return await page.evaluate(expression, timeout=timeout_ms)
    except Exception as e:
        raise EvalError(f"脚本报错：{type(e).__name__}: {str(e)[:200]}") from None


def render(value: Any) -> str:
    """把求值结果渲染成给 agent 看的文本（截断）。"""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=1)
        except Exception:
            text = str(value)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n…（已截断，共 {len(text)} 字）"
    return text
