"""守护进程自检：证明**"MCP 服务进程被回收，浏览器窗口还活着"**。

这条不自检，"人在回合之间设筛选条件"的流程就是断的（实测踩过：窗口随进程一起没了）。

步骤：
 ① 子进程里连守护进程 → 打开夹具页 → **进程退出**（模拟宿主回收 MCP 服务）
 ② 主进程新建客户端 → snapshot：必须拿到**同一个页面**（而不是重新开窗）
 ③ 核对：用本身份 profile 的 msedge 进程仍是 1 个（没被重开）
 ④ browse_close → 守护进程退出、管道消失、窗口关闭

跑法：runtime\\python\\python.exe app\\selftest\\daemon_smoke.py   （Windows；会开一个 Edge 窗口）
"""
from __future__ import annotations

import asyncio
import http.server
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 控制台/管道默认 GBK，中文与 ✓ 会炸——统一切 UTF-8 输出（与 smoke.py 同款处理）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent_browser import ipc                              # noqa: E402
from agent_browser.client import DaemonClient              # noqa: E402

IDENTITY = "daemon_test"
FIXTURE = Path(__file__).parent / "fixtures"
PORT = 8793


def serve() -> socketserver.TCPServer:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(FIXTURE), **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def browser_pids() -> set[str]:
    """系统里"用本身份 profile"的 msedge 进程号（Windows）。"""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(["wmic", "process", "where", "name='msedge.exe'",
                              "get", "ProcessId,CommandLine", "/format:csv"],
                             capture_output=True, text=True, timeout=25).stdout
    except Exception:
        return set()
    pids = set()
    for line in out.splitlines():
        if IDENTITY in line:
            pids.add(line.rstrip().rsplit(",", 1)[-1])
    return pids


def pipe_alive() -> bool:
    from multiprocessing.connection import Client
    try:
        c = Client(ipc.address(IDENTITY), family=ipc.family())
        c.close()
        return True
    except Exception:
        return False


async def main() -> int:
    ok = True
    httpd = serve()
    url = f"http://127.0.0.1:{PORT}/selftest_page.html"
    here = Path(__file__).resolve().parent
    try:
        print("① 子进程里打开页面，然后进程退出（模拟 MCP 服务被回收）…")
        # 结果走文件、**不抓管道**：守护进程是从子进程拉起的，抓管道会让父进程一直等到管道关闭
        result_file = Path(tempfile.gettempdir()) / "real_browser_daemon_check.txt"
        result_file.unlink(missing_ok=True)
        proc = subprocess.run([sys.executable, str(here / "_daemon_open_once.py"),
                               url, str(result_file)], timeout=180)
        assert proc.returncode == 0 and result_file.exists(), f"子进程没跑通：{proc.returncode}"
        line = result_file.read_text(encoding="utf-8").strip().splitlines()
        assert line and line[0].startswith("OPENED"), f"子进程输出不对：{line}"
        print(f"  ✓ {line[0]}")
        pids_before = browser_pids()
        print(f"  · 此刻用本身份的浏览器进程：{sorted(pids_before)}")

        print("② 新客户端直接取快照：应还是**同一个页面**（没重新开窗）")
        t0 = time.time()
        client = DaemonClient(IDENTITY, site="default", allow_local=True)
        text = await client.snapshot()
        assert url in text, f"不是原来那个页面：\n{text[:300]}"
        assert "可交互：" in text, text[:200]
        print(f"  ✓ 拿到同一页面（{time.time()-t0:.1f}s），并有编号快照")

        pids_after = browser_pids()
        print(f"  · 现在的浏览器进程：{sorted(pids_after)}")
        assert pids_after == pids_before and pids_after, "浏览器被重开了（说明窗口没被守护住）"
        print("  ✓ 浏览器进程没变 → 窗口被守护住了")

        print("③ 滚动：应带上「元素数变化」（这次会真的改页面）")
        out = await client.scroll("down", 400)
        assert "页面可交互元素" in out, out[:200]
        print("  ✓", out.replace("\n", " ｜ "))

        print("④ 收尾：browse_close → 守护进程退出、管道消失、窗口关闭")
        print("  ·", (await client.close()).splitlines()[0])
        for _ in range(20):
            if not pipe_alive() and not browser_pids():
                break
            await asyncio.sleep(0.5)
        assert not pipe_alive(), "守护进程还在（管道还在）"
        assert not browser_pids(), f"浏览器没退干净：{browser_pids()}"
        print("  ✓ 守护进程已退出，窗口已关闭")
    except AssertionError as e:
        ok = False
        print("✗ 断言失败：", e)
    except Exception as e:                                  # noqa: BLE001
        ok = False
        print("✗ 异常：", type(e).__name__, e)
    finally:
        httpd.shutdown()
    print("\n结论：", "全部通过 ✓" if ok else "有失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
