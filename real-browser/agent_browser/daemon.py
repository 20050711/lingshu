"""常驻守护进程：持有浏览器，让 MCP 服务进程可以来去自由。

**为什么需要它**（2026-09-24 实测）：宿主程序会在回合之间回收 MCP 服务进程。
浏览器由那个进程持有的话，进程一没窗口就跟着没——而"扫完码我再设筛选条件"这种
人机交接，恰恰要求窗口在**回合之间活着**。把浏览器交给本进程持有后，
MCP 服务被回收只是"话筒掉了"：窗口、登录态、当前页面都还在，下次连上来接着用。

设计：
- **一身份一进程**（管道名带身份名）；profile 文件锁也由它持有（人机同窗，不会两边抢）。
- 协议：`{method, args, kwargs}` → `{ok, result}` / `{ok: false, type, error}`，两端各一次 send/recv。
- 退出条件：`browse_close` / 人把窗口关了（下次请求时自愈复位）/ 显式 `shutdown`。
- 日志：`<home>/logs/daemon-<身份>.log`（出问题先看它）。

手工排查：
    python -m agent_browser.daemon --identity tardis --site default   # 前台跑，日志直接打屏幕
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
import traceback
from multiprocessing.connection import Listener

from . import ipc
from .browser_agent import BrowserAgent
from .config import idle_close_s
from .ipc import METHODS

CALL_TIMEOUT = 300          # 单次调用上限（含 30s 的 goto 与拟人停顿）


def _log(msg: str, path) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:                                     # 屏幕输出可能落在 GBK 句柄上——日志绝不能因此中断
        print(line, flush=True)
    except Exception:                        # noqa: BLE001
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _dispatch(loop, agent: BrowserAgent, req: dict) -> dict:
    """在事件循环上执行一次工具调用（本函数跑在接客线程里，所以走 run_coroutine_threadsafe）。"""
    method = str(req.get("method") or "")
    if method not in METHODS:
        return {"ok": False, "type": "GuardError", "error": f"守护进程不认识这个方法：{method}"}
    fn = getattr(agent, method, None)
    if fn is None:
        return {"ok": False, "type": "GuardError", "error": f"守护进程没有这个方法：{method}"}
    try:
        fut = asyncio.run_coroutine_threadsafe(
            fn(*req.get("args") or [], **(req.get("kwargs") or {})), loop)
        return {"ok": True, "result": fut.result(timeout=CALL_TIMEOUT)}
    except Exception as e:                                   # noqa: BLE001 —— 原样回传给客户端
        return {"ok": False, "type": type(e).__name__, "error": f"{type(e).__name__}: {e}"}


def _serve_conn(loop, agent, conn, log, state: dict) -> bool:
    """处理一个连接。返回 True 表示该收摊了（browse_close / shutdown）。"""
    while True:
        try:
            req = conn.recv()
        except (EOFError, OSError):
            return False                                     # 客户端走了（MCP 服务被回收）——窗口留着
        if not isinstance(req, dict):
            conn.send({"ok": False, "type": "GuardError", "error": "请求格式不对"})
            continue
        method = req.get("method")
        if method == "ping":
            conn.send({"ok": True, "result": "pong"})
            continue
        if method == "shutdown":
            conn.send({"ok": True, "result": "守护进程退出"})
            return True
        state["last_call"] = time.time()      # 每次真实调用都续期（ping 不算）
        asyncio.run_coroutine_threadsafe(_heal(agent, log), loop).result(timeout=10)
        resp = _dispatch(loop, agent, req)
        _log(f"{method} → {'ok' if resp['ok'] else resp.get('error')}", log)
        try:
            conn.send(resp)
        except OSError:
            return False
        if method == "close":
            return True                                      # 显式收尾 → 守护进程一起退


async def _heal(agent: BrowserAgent, log) -> None:
    """人把窗口关了？复位状态，下次调用会重新开窗（而不是抛 Target closed）。"""
    try:
        await agent.ensure_alive()
    except Exception as e:                                   # noqa: BLE001
        _log(f"ensure_alive 出错（忽略）：{e}", log)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="real-browser daemon")
    ap.add_argument("--identity", required=True)
    ap.add_argument("--site", default=None)
    ap.add_argument("--allow-local", action="store_true")
    args = ap.parse_args(argv)

    log = ipc.log_path(args.identity)
    addr = ipc.address(args.identity)
    _log(f"守护进程启动 pid={os.getpid()} 管道={addr} site={args.site}", log)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    agent = BrowserAgent(args.identity, site=args.site, allow_local=args.allow_local)

    stop = threading.Event()
    state = {"last_call": time.time()}          # 空闲自动关闭的计时基准

    def accept_loop() -> None:
        try:
            with Listener(addr, family=ipc.family()) as listener:
                while not stop.is_set():
                    try:
                        conn = listener.accept()
                    except (OSError, EOFError):
                        break
                    with conn:
                        if _serve_conn(loop, agent, conn, log, state):
                            break
        except Exception as e:                               # noqa: BLE001
            _log(f"接客循环异常退出：{type(e).__name__}: {e}\n{traceback.format_exc()}", log)
        finally:
            stop.set()

    threading.Thread(target=accept_loop, daemon=True).start()

    async def waiter() -> None:
        last_save = time.time()
        idle_limit = idle_close_s()
        while not stop.is_set():
            await asyncio.sleep(0.5)
            # 每 60 秒顺手把 cookie 落一次盘：人在窗口里扫码登录之后，票据不能因为意外丢
            if time.time() - last_save > 60:
                last_save = time.time()
                try:
                    await agent.save_state()
                except Exception:                            # noqa: BLE001
                    pass
            # 空闲自动收尾（2026-09-28）：智能体跑完没调 browse_close 时，窗口别一直留在桌面上
            if idle_limit > 0 and agent.is_open and time.time() - state["last_call"] > idle_limit:
                _log(f"空闲超过 {idle_limit}s 无调用 → 自动关闭浏览器（登录态落盘）", log)
                try:
                    await agent.close()
                except Exception as e:                       # noqa: BLE001
                    _log(f"自动关闭出错（忽略）：{e}", log)
                stop.set()                                   # 守护进程一并退出；下次调用冷启动重开

    try:
        loop.run_until_complete(waiter())
    except KeyboardInterrupt:
        pass
    finally:
        try:
            loop.run_until_complete(agent.close())           # 收摊：窗口关掉、登录态落盘
        except Exception:                                    # noqa: BLE001
            pass
        _log("守护进程退出", log)
        if os.name != "nt":
            try:
                os.unlink(addr)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
