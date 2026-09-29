"""会话级基建：profile 单实例互斥 + **cookie 的存与恢复**（进程退出自动释放锁）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import config


class ProfileLock:
    """同一个 profile 同一时刻只允许一个会话使用（人工开着窗口时，agent 不会同时用）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write("x")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise RuntimeError(
                f"该身份的浏览器正被另一个进程使用（{self.path}）——"
                "同一身份同一时刻只允许一个会话；若是人工开着窗口，请先关窗或人工处理完再让 agent 接手"
            ) from None
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


# ---------- cookie 的存与恢复 ----------
#
# 为什么必须自己存（2026-09-24 实测踩到）：Chromium 退出时会丢掉**会话级 cookie**
# （不少站点的会话票据就是会话级 cookie），而"登录一次、以后都在"恰恰靠它们。
# 所以退出前把 context 的全部 cookie（含会话级，expires=-1）落盘，下次开窗前灌回去。

def cookie_file(identity_name: str) -> Path:
    d = config.home_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{identity_name}.cookies.json"


async def dump_cookies(ctx, identity_name: str) -> int:
    """把当前上下文的所有 cookie 落盘；返回条数。"""
    try:
        cookies = await ctx.cookies()
    except Exception:
        return 0
    if not cookies:
        return 0
    p = cookie_file(identity_name)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)                      # 原子替换，写一半也不怕
        try:
            os.chmod(p, 0o600)                  # 里面是登录票据，只给本人读
        except OSError:
            pass
    except OSError:
        return 0
    return len(cookies)


async def load_cookies(ctx, identity_name: str) -> int:
    """把上次落盘的 cookie 灌回新开的上下文；返回条数（没有就是 0）。"""
    p = cookie_file(identity_name)
    if not p.is_file():
        return 0
    try:
        cookies = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(cookies, list) or not cookies:
            return 0
        await ctx.add_cookies(cookies)
        return len(cookies)
    except Exception:
        return 0
