"""MCP 工具面（stdio / streamable-http）：给任意 agent 驱动真实浏览器。

启动：
  stdio（本地宿主程序直连）
    ``python -m agent_browser.mcp_server --identity <身份名> [--site <站点档案>]``
  HTTP（平台后端经 MCP 协议接入，见 tardis 平台「MCP 工具」页）
    ``python -m agent_browser.mcp_server --identity <身份名> --transport streamable-http --port 18130``
护栏与拟人内建、**不可由 agent 关闭**；错误以可操作话术返回，不抛堆栈。
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from mcp.server.mcpserver import MCPServer

from .client import DaemonClient
from .guard import GuardError

INSTRUCTIONS = """real-browser：驱动本机**真实浏览器**（系统 Edge/Chrome，有头窗口）做网页操作。

【工具选择】平台自带的搜索/HTTP 工具能拿到的信息，不要开浏览器；只有需要登录态、需要交互、
或站点反爬严格时，才用本工具。

【基本循环】browse_open(网址) → 返回带编号 [n] 的页面快照 → browse_click(n) / browse_type(n, 文本)。
**编号在每次快照后刷新**：页面一变（点过、滚过、跳过）就必须先 browse_snapshot 重新取号，
拿着旧编号点会点到别的地方。

【动态渲染站点（单页应用）】
1. **"导航返回"不等于"内容就绪"**：等目标内容出现再动手——
   `browse_wait("一段可见文字")` 或 `browse_wait("css=选择器")`（有界；超时≠内容不存在）。
   快照开头的"可交互：N 个"是判断页面变没变的尺子；**滚动后要重新取快照**，条数没变多半是还没加载完。
2. **快照里找不到目标元素时别绕页面**：用 browse_extract 拿到它的 CSS 选择器（读），
   再用 browse_click(selector="…") 点它（写）——和用编号点一样安全，护栏一样在。

【越用越顺】在某个站点**亲眼验证过**的事实（URL 结构、该点哪个入口、额度提示原文…），
用 `browse_remember("…")` 记一笔——下次进这个站点会自动顶在快照开头。只记验证过的，别记猜测。

【规模】工具**不设**"每小时多少次"的配额——"做多少"靠你自觉：一天的对外动作（打招呼/投递）
别超过 30~50 个，每轮按用户给的上限（没说就 10 个），到数就停。**规模比技术痕迹更致命。**

【三条硬纪律】
1. **点完必须核对返回里的"状态"行**（URL / 列表条数变化）。与预期不符就停手，先 browse_snapshot
   看清页面，再决定下一步——不要接着盲点。
2. **browse_eval / browse_extract 只能用来读**（取值、找元素、抽结构化数据；跑在隔离世界，页面看不见）。
   **绝不能用它们点击或输入**：JS 合成事件 isTrusted=false，站点一眼认出。
3. 遇到"被护栏拒绝""需要验证""跳到 about:blank"：按提示处理或调 browse_ask_human 叫人，
   **不要原样重试**。

【账号资料】可能被配成"共用用户日常浏览器的资料目录"（零扫码）：若工具报"你的日常浏览器正在运行"，
**先跟用户确认**，再让他自己关、或调 browse_takeover() 帮关。
另注意：不少站点限制**同一账号只能一个网页端在线**——你这边登录会顶掉用户在别处的登录，登录前先跟他说清。

【要人工介入时】browse_ask_human(原因) 会在**当前这个浏览器窗口**停手等人（登录扫码/验证码/滑块），
人在窗口里操作完，你再调 browse_resume 继续。

【收尾】任务做完就 browse_close：登录态落盘、窗口关掉、常驻进程退出。**别让窗口一直开在用户电脑上。**"""


def build(agent: DaemonClient) -> MCPServer:
    server = MCPServer(name="real-browser", instructions=INSTRUCTIONS)

    async def _safe(fn, *args, **kw) -> str:
        try:
            return await fn(*args, **kw)
        except GuardError as e:
            return f"被护栏拒绝：{e}"
        except Exception as e:
            return f"操作失败：{type(e).__name__}: {str(e)[:300]}"

    # ---- 原子操作 ----

    @server.tool()
    async def browse_open(url: str) -> str:
        """打开网址并返回带编号的页面快照。"""
        return await _safe(agent.open, url)

    @server.tool()
    async def browse_snapshot() -> str:
        """重新获取当前页面的编号快照（页面变化后必须重新取号）。"""
        return await _safe(agent.snapshot)

    @server.tool()
    async def browse_click(ref: int | None = None, selector: str | None = None,
                           path: str = "auto") -> str:
        """点击元素；返回本次点击的状态变化与新快照。

        **两种指定方式，二选一**：
        - `ref`：快照里的编号 [n]（首选）。
        - `selector`：CSS 选择器。**快照里没有的元素走这条**——比如动态渲染站点里
          无 href 的 `<a>`、悬停才出现的按钮。先用 browse_extract 把它的选择器找出来，
          再用本参数点它（不要用 browse_eval 去点，那是 JS 合成事件，站点一眼认出）。

        path：auto（默认，按元素类型自动）| curve（拟人曲线）| direct（低掠过，少触发 hover）
        | instant（不做前置移动，直接点）。"""
        return await _safe(agent.click, ref, selector or "", path)

    @server.tool()
    async def browse_type(ref: int, text: str, submit: bool = False) -> str:
        """在编号 [ref] 的输入框逐键输入；submit=True 时随后回车提交。"""
        return await _safe(agent.type_text, ref, text, submit)

    @server.tool()
    async def browse_scroll(direction: str = "down", amount: int = 600) -> str:
        """拟人滚动页面；direction 为 down 或 up，amount 为像素。"""
        return await _safe(agent.scroll, direction, amount)

    @server.tool()
    async def browse_press(key: str) -> str:
        """按键（如 Enter / Escape / Tab / PageDown）。"""
        return await _safe(agent.press, key)

    @server.tool()
    async def browse_back() -> str:
        """浏览器后退，并返回新快照。"""
        return await _safe(agent.back)

    @server.tool()
    async def browse_text(limit: int = 6000) -> str:
        """当前页面可见正文（截断）。"""
        return await _safe(agent.text, limit)

    @server.tool()
    async def browse_takeover() -> str:
        """（共用用户资料目录模式）用户同意后，帮他把日常浏览器优雅关掉，让出资料目录。

        **调用前必须先跟用户确认**——那是他自己的浏览器。只发关闭信号（不强制结束）；
        关不掉会如实告诉你（通常是弹了"要关闭所有标签页吗"的确认框）。
        工具报"你的日常浏览器正在运行"时，就是该用它（或让用户自己关）的时候。
        """
        return await _safe(agent.takeover)

    @server.tool()
    async def browse_remember(fact: str) -> str:
        """把一条**验证过的**站点经验记下来（按域名存；下次 open 到该站点会自动顶在快照开头）。

        记什么：URL 结构、该点哪个入口、哪些按钮有副作用、额度/风控提示的原文……
        **只记亲眼验证过的事实，别记猜测**；每条会带上日期。
        """
        return await _safe(agent.remember, fact)

    @server.tool()
    async def browse_screenshot() -> str:
        """当前页面截图并返回文件路径（留证用）。"""
        return await _safe(agent.screenshot)

    @server.tool()
    async def browse_download(ref: int) -> str:
        """点击编号 [ref] 触发下载，保存到本机并返回文件路径。"""
        return await _safe(agent.download, ref)

    # ---- 开放读取通道（只读） ----

    @server.tool()
    async def browse_eval(expression: str, world: str = "isolated") -> str:
        """在页面里执行 JS **读取**内容并返回结果（默认跑在隔离世界，页面看不见）。
        只能读，不能用来点击/输入。示例：document.querySelectorAll('.card').length"""
        return await _safe(agent.eval_read, expression, world)

    @server.tool()
    async def browse_extract(expression: str) -> str:
        """结构化读取：表达式须返回 JSON 字符串（用 JSON.stringify(...) 包一层），返回格式化结果。"""
        return await _safe(agent.extract, expression)

    # ---- 人机交接 ----

    @server.tool()
    async def browse_wait(until: str, timeout_sec: int = 15) -> str:
        """等页面上出现目标内容再继续（**"导航返回"不等于"内容就绪"**）。

        · `until` 给一段**可见文字**，或 `css=选择器`。
        · 有界等待（默认 15 秒，最多 60），到点不报错，而是如实告诉你"还没出现"+ 当前快照。
        · **超时 ≠ 内容不存在**：可能还在加载、或在验证/登录跳转这类中间态。
        · 典型用法：`browse_open` 之后先 `browse_wait("职位推荐")`，等真东西出来再动手。
        """
        return await _safe(agent.wait_for, until, timeout_sec)

    @server.tool()
    async def browse_login_status() -> str:
        """检查当前站点的登录态（判据来自站点档案；未配置时给做法）。"""
        return await _safe(agent.login_status)

    @server.tool()
    async def browse_ask_human(reason: str) -> str:
        """停手并请人工在当前浏览器窗口里处理（登录/验证码/风控提示），处理完调 browse_resume。"""
        return await _safe(agent.ask_human, reason)

    @server.tool()
    async def browse_resume() -> str:
        """人工处理完了，恢复操作（会先返回当前页面快照）。"""
        return await _safe(agent.resume)

    @server.tool()
    async def browse_close() -> str:
        """优雅关闭浏览器（登录态落盘）。任务结束后调用。"""
        return await _safe(agent.close)

    return server


def _silence_shutdown_noise() -> None:
    """Windows 上 asyncio 退出时会给"未关闭的 pipe transport"打一堆 Exception ignored——
    那是解释器收尾噪音（__repr__ 访问已关闭的 fd），客户端会把 stderr 当日志读，故静音。"""
    def hook(unraisable):
        if "closed pipe" in str(unraisable.exc_value):
            return
        sys.__unraisablehook__(unraisable)
    sys.unraisablehook = hook


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="real-browser mcp")
    ap.add_argument("--identity", required=True, help="身份名（一账号一 profile）")
    ap.add_argument("--site", default=None, help="站点档案名（默认 default）")
    ap.add_argument("--allow-local", action="store_true",
                    help="允许访问本机地址（仅本地测试用，生产不要开）")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"],
                    help="stdio=本地宿主直连；streamable-http=以 HTTP 服务暴露给平台后端")
    ap.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址（仅 streamable-http）")
    ap.add_argument("--port", type=int, default=18130, help="HTTP 监听端口（仅 streamable-http）")
    args = ap.parse_args(argv)
    _silence_shutdown_noise()

    # 浏览器交给常驻守护进程持有：宿主回收本进程时，窗口与人机交接不受影响（见 daemon.py）
    agent = DaemonClient(args.identity, site=args.site, allow_local=args.allow_local)
    server = build(agent)
    try:
        if args.transport == "streamable-http":
            # 平台后端（services/mcp_client.py）走 Streamable HTTP：POST http://host:port/mcp
            server.run("streamable-http", host=args.host, port=args.port)
        else:
            server.run()  # stdio
    finally:
        # 收尾用 close_if_local：进程内模式关浏览器；守护进程模式**什么都不做**
        # ——宿主回收本进程时，浏览器必须留着（2026-09-24 实测：这里直接 close 会把窗口一起关掉）
        try:
            asyncio.run(agent.close_if_local())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
