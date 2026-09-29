"""回归：**MCP 服务进程退出后，浏览器必须还活着**（守护进程要真的生效）。

背景（2026-09-24 实测踩到）：宿主程序一回合结束就回收 MCP 服务进程。
当时有两处让它变成"连浏览器一起关掉"：
 ① `mcp_server` 的 finally 直接调 `close()` —— 被转发给守护进程，等于主动收摊；
 ② 守护进程是它的子进程，宿主清理进程树时被连根拔起（改用 WMI/CIM 创建进程解决）。
这个脚本就按真实顺序演一遍，两条都覆盖。

跑法：runtime\\python\\python.exe app\\selftest\\mcp_exit_check.py   （Windows，会开一个 Edge 窗口）
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent_browser import ipc                                  # noqa: E402
from agent_browser.client import DaemonClient                  # noqa: E402

IDENTITY = "mcp_exit_test"
FIXTURE = Path(__file__).parent / "fixtures"
PORT = 8795


def serve() -> socketserver.TCPServer:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(FIXTURE), **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def browser_alive() -> int:
    """用本身份 profile 的 msedge 进程数（>0 就说明窗口还在）。"""
    if os.name != "nt":
        return 0
    wmic = r"C:\Windows\System32\wbem\WMIC.exe"
    try:
        out = subprocess.run([wmic, "process", "where", "name='msedge.exe'",
                              "get", "CommandLine", "/format:list"],
                             capture_output=True, text=True, timeout=25).stdout
    except Exception:
        return 0
    return sum(1 for ln in out.splitlines() if IDENTITY in ln)


def rpc(proc, obj: dict, want_id: int, timeout: float = 120.0) -> dict:
    proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP 服务提前退出了")
        line = line.strip()
        if not line.startswith("{"):
            continue
        msg = json.loads(line)
        if msg.get("id") == want_id:
            return msg
    raise RuntimeError(f"等 {want_id} 的应答超时")


async def main() -> int:
    httpd = serve()
    url = f"http://127.0.0.1:{PORT}/selftest_page.html"
    ok = True
    py = sys.executable
    try:
        print("① 起一个**真实的 MCP 服务进程**，让它开一个页面…")
        proc = subprocess.Popen(
            [py, "-m", "agent_browser.mcp_server", "--identity", IDENTITY,
             "--site", "default", "--allow-local"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "exit-test", "version": "1"}}}, 1)
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        resp = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "browse_open", "arguments": {"url": url}}}, 2)
        text = json.dumps(resp, ensure_ascii=False)
        assert url in text, f"没打开页面：{text[:300]}"
        print("  ✓ 页面已打开；此刻浏览器进程数 =", browser_alive())
        tail = ipc.log_path(IDENTITY).read_text(encoding="utf-8").strip().splitlines()[-6:]
        how = next((ln for ln in reversed(tail) if "拉起守护进程" in ln), "?")
        print("  · 守护进程怎么起的：", how.split("]")[-1].strip()[:90])
        assert "（wmic）" in how or "（cim）" in how, f"没走 WMI/CIM，进程树清理时会一起被带走：{how}"

        print("② 模拟宿主回收：关掉它的 stdin，让 MCP 服务退出…")
        proc.stdin.close()
        proc.wait(timeout=60)
        print(f"  ✓ MCP 服务已退出（退出码 {proc.returncode}）")

        print("③ 关键断言：浏览器和守护进程必须还活着")
        time.sleep(2)
        alive = browser_alive()
        assert alive > 0, "浏览器被一起关掉了——守护进程没生效 ✗"
        print(f"  ✓ 浏览器还在（{alive} 个进程）")
        client = DaemonClient(IDENTITY, site="default", allow_local=True)
        snap = await client.snapshot()
        assert url in snap, f"接管到的不是原来那个页面：{snap[:200]}"
        print("  ✓ 新客户端能接上，页面还是原来那个")

        print("④ 收尾：只有显式 browse_close 才收摊")
        print("  ·", (await client.close()).splitlines()[0])
    except AssertionError as e:
        ok = False
        print("✗ 断言失败：", e)
    except Exception as e:                                          # noqa: BLE001
        ok = False
        print("✗ 异常：", type(e).__name__, e)
    finally:
        httpd.shutdown()
        try:
            DaemonClient(IDENTITY, site="default", allow_local=True)._call_daemon("shutdown")
        except Exception:                                           # noqa: BLE001
            pass
    print("\n结论：", "全部通过 ✓" if ok else "有失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
