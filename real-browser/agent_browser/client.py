"""MCP 服务侧的瘦客户端：把工具调用转发给常驻守护进程。

宿主程序在回合之间回收 **MCP 服务进程**是它自己的调度，我们管不了；
但**浏览器窗口**由守护进程持有，所以"回收"只是掉了话筒，不影响人机交接
（人还能在窗口里扫码、设筛选条件）。详见 `daemon.py` 开头。

- **惰性连接**：第一次调用才连；连不上就把守护进程拉起来再连。
- **兜底**：守护进程实在起不来 → 退回进程内直跑（功能一样，只是窗口扛不住进程回收），并写日志。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from multiprocessing.connection import Client
from pathlib import Path

from . import ipc
from .browser_agent import BrowserAgent
from .guard import GuardError


def _log_line(identity: str, text: str) -> None:
    try:
        with open(ipc.log_path(identity), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except OSError:
        pass


class DaemonUnavailable(RuntimeError):
    """连不上、也拉不起守护进程——这时才退回进程内模式。"""


class DaemonClient:
    """与 ``BrowserAgent`` 同形（同名方法），MCP 层不用改一行。"""

    def __init__(self, identity: str, site: str | None = None, *,
                 allow_local: bool = False, connect_timeout: float = 25.0) -> None:
        self.identity = identity
        self.site = site
        self.allow_local = allow_local
        self.connect_timeout = connect_timeout
        self._conn = None
        self._fallback: BrowserAgent | None = None

    # ---------------- 连接 ----------------

    def _try_connect(self):
        try:
            return Client(ipc.address(self.identity), family=ipc.family())
        except (OSError, EOFError):
            return None

    def _spawn_detached(self, args: list[str]) -> str:
        """拉起守护进程，**并让它活过宿主对进程树的清理**。

        实测（2026-09-24）：宿主程序在一回合结束时回收 MCP 服务，会把它的**整个子进程树**
        一起清掉——普通 Popen（哪怕带 DETACHED_PROCESS）也被带走，于是浏览器又跟着没了。
        所以优先用 **WMI / CIM 创建进程**：它的父进程是 WmiPrvSE 而不是我们，天然不在那棵树里。
        返回实际用了哪条路（写进日志，排障时一眼能看出有没有真正脱离）。
        """
        cmdline = subprocess.list2cmdline(args)
        cwd = str(Path(sys.executable).parent)
        root = Path(os.environ.get("SystemRoot", r"C:\Windows"))

        wmic = root / "System32" / "wbem" / "WMIC.exe"
        if wmic.is_file():
            try:
                r = subprocess.run([str(wmic), "process", "call", "create", cmdline,
                                    "startup_directory", cwd],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and "ProcessId" in (r.stdout or ""):
                    return "wmic"
            except Exception as e:                                   # noqa: BLE001
                _log_line(self.identity, f"wmic 拉起守护进程失败：{e}")
        if "'" not in cmdline and '"' not in cmdline:
            ps = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if ps.is_file():
                script = (f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                          f"-Arguments @{{CommandLine='{cmdline}'; CurrentDirectory='{cwd}'}} | "
                          f"Select-Object -ExpandProperty ProcessId")
                try:
                    r = subprocess.run([str(ps), "-NoProfile", "-Command", script],
                                       capture_output=True, text=True, timeout=45)
                    if r.returncode == 0 and (r.stdout or "").strip().isdigit():
                        return "cim"
                except Exception as e:                               # noqa: BLE001
                    _log_line(self.identity, f"CIM 拉起守护进程失败：{e}")
        # 兜底：普通脱离进程（宿主清理进程树时会被带走——功能可用，只是窗口活不过回合）
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        log = ipc.log_path(self.identity)
        with open(log, "ab") as lf:
            # 显式 UTF-8：宿主是原生 Windows 环境，子进程 stdout 默认 GBK，中文日志会把它打崩
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                             close_fds=True, creationflags=flags,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return "popen"

    def _spawn(self) -> None:
        args = [sys.executable, "-m", "agent_browser.daemon", "--identity", self.identity]
        if self.site:
            args += ["--site", self.site]
        if self.allow_local:
            args += ["--allow-local"]
        how = self._spawn_detached(args)
        _log_line(self.identity, f"MCP 服务拉起守护进程（{how}）：{' '.join(args)}")

    def _connect(self) -> None:
        conn = self._try_connect()
        if conn is None:
            self._spawn()
            deadline = time.time() + self.connect_timeout
            while conn is None and time.time() < deadline:
                time.sleep(0.3)
                conn = self._try_connect()
        if conn is None:
            raise DaemonUnavailable(
                f"守护进程没起来（等了 {self.connect_timeout:.0f}s）"
                f"——看日志：{ipc.log_path(self.identity)}")
        self._conn = conn

    # ---------------- 调用 ----------------

    def _call_daemon(self, method: str, *args, **kwargs):
        if self._conn is None:
            self._connect()
        payload = {"method": method, "args": list(args), "kwargs": kwargs}
        resp = None
        for attempt in (1, 2):                   # 守护进程中途没了（比如它自己退了）→ 重连一次
            try:
                self._conn.send(payload)
                resp = self._conn.recv()
                break
            except (EOFError, OSError):
                self._conn = None
                if attempt == 2:
                    raise DaemonUnavailable("守护进程连接中断") from None
                self._connect()
        if not isinstance(resp, dict):
            raise DaemonUnavailable("守护进程返回了无法识别的内容")
        if not resp.get("ok"):
            err = str(resp.get("error") or "未知错误")
            if resp.get("type") == "GuardError":
                raise GuardError(err)
            raise RuntimeError(err)
        return resp.get("result")

    async def close_if_local(self) -> None:
        """本进程要退了——**什么都不做**。

        MCP 服务被宿主回收是常态（实测：一回合结束就回收），而浏览器必须留在守护进程里；
        只有 agent 显式调 `browse_close` 才收摊。这里绝不能把 close 转发过去。
        """
        return None

    async def _dispatch(self, name: str, *args, **kwargs):
        if self._fallback is not None:
            return await getattr(self._fallback, name)(*args, **kwargs)
        try:
            return await asyncio.to_thread(self._call_daemon, name, *args, **kwargs)
        except DaemonUnavailable as e:
            # 退回进程内直跑：功能一样，只是"窗口扛不住宿主回收进程"这个毛病治不了
            self._fallback = BrowserAgent(self.identity, site=self.site,
                                          allow_local=self.allow_local)
            try:
                with open(ipc.log_path(self.identity), "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 守护进程不可用（{e}），"
                            f"退回进程内模式——本进程被回收时窗口会跟着关\n")
            except OSError:
                pass
            return await getattr(self._fallback, name)(*args, **kwargs)

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in ipc.METHODS:
            raise AttributeError(f"{type(self).__name__} 没有 {name}")
        async def call(*a, **kw):
            return await self._dispatch(name, *a, **kw)
        return call
