"""MCP 服务 ↔ 守护进程 的本地 IPC 地址（**不开任何 TCP 端口**）。

- Windows：命名管道 `\\\\.\\pipe\\real-browser-<身份>`
- macOS / Linux：`<home>/<身份>.sock`（Unix socket）

两端都用 stdlib 的 `multiprocessing.connection`（`AF_PIPE` / `AF_UNIX`），不引额外依赖；
管道名/路径里带身份名，天然做到"一身份一守护进程"。
"""
from __future__ import annotations

import os

from . import config

PREFIX = "real-browser"

# 允许在 MCP 服务 ↔ 守护进程之间转发的工具方法（= 工具面 + 生命周期），白名单，别的一律拒
METHODS = {
    "open", "snapshot", "resnapshot", "click", "type_text", "scroll", "press", "back",
    "text", "screenshot", "download", "eval_read", "extract", "wait_for", "remember",
    "login_status", "ask_human", "resume", "close", "takeover",
}


def family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def address(identity: str) -> str:
    if os.name == "nt":
        return rf"\\.\pipe\{PREFIX}-{identity}"
    home = config.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    return str(home / f"{PREFIX}-{identity}.sock")


def log_path(identity: str):
    d = config.home_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"daemon-{identity}.log"
