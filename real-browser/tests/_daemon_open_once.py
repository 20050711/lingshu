"""辅助脚本：在一个**独立、跑完立刻退出**的进程里连守护进程并打开一个网址。

用途：模拟"宿主程序回收 MCP 服务进程"——注意**故意不调 close**，
因为这个进程就是被强杀/回收的那种角色。由 tests/daemon_smoke.py 调用。
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from agent_browser.client import DaemonClient  # noqa: E402


async def main() -> None:
    url = sys.argv[1]
    client = DaemonClient("daemon_test", site="default", allow_local=True)
    out = await client.open(url)
    line = "OPENED " + out.splitlines()[0].strip()
    print(line)
    if len(sys.argv) > 2:                      # 结果另写一份文件：调用方不必抓管道（见 daemon_smoke 注释）
        from pathlib import Path
        Path(sys.argv[2]).write_text(line + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
