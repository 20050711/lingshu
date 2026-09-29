"""启动与会话：**真机真浏览器**（系统 Edge / Chrome），启动卫生在此收口。

与本项目的三条硬规则（方案 §五）对应：
1. **不开任何调试端口**：Playwright/patchright 默认走 `--remote-debugging-pipe`；
   本文件对启动参数做防呆检查，出现 remote-debugging-* 直接抛错。
2. **不伪造任何东西**：不设 UA、不设 locale/timezone（真机就是真的）、不加"社区偏方"flag。
3. **窗口最大化 + 置前台**：真实用户窗口的样子；也让页面拿到真实 `outerWidth/Height`
   （后台/小窗会让 `outer` 变成 0x0 —— 自动化招牌之一，2026-09-24 实测）。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .identity import Identity
from .session import ProfileLock, load_cookies

# 启动卫生：这些参数一律禁止出现在**我们自己拼的**命令行里。
#
# 注意（2026-09-24 实测）：`--disable-blink-features=AutomationControlled` 是 **patchright 驱动
# 自带的默认参数**（driver/package/lib/coreBundle.js），而且它是 `navigator.webdriver=false`
# 的唯一机制——A/B 对照实测：带它 webdriver=False，用 ignore_default_args 去掉它立刻变 True。
# 所以这条防线只保证"我们不额外加"，**不代表命令行里没有它**；Edge 的"不受支持的命令行标志"
# 警告条正来自这里，属于必须接受的代价（页面 JS 读不到命令行参数）。
FORBIDDEN = ("--enable-automation", "--remote-debugging-port", "--remote-debugging-address",
             "--disable-blink-features=AutomationControlled", "--no-sandbox", "--user-agent")


def build_args(ident: Identity, profile: dict[str, Any]) -> list[str]:
    args = [
        "--start-maximized",                 # 真用户窗口的样子（也是 outer 正确的必要条件）
        "--hide-crash-restore-bubble",       # 异常退出后不弹"恢复页面"气泡（那是给人看的异常痕迹）
    ]
    if ident.proxy:
        args.append(f"--proxy-server={ident.proxy}")
    bad = [a for a in args for p in FORBIDDEN if a.startswith(p)]
    if bad:  # 防呆：将来加参数时立刻炸，而不是静默带上
        raise ValueError(f"启动参数违反卫生规则：{bad}")
    return args


@dataclass
class Launched:
    pw: Any
    ctx: Any
    page: Any
    lock: ProfileLock
    channel: str


async def launch(ident: Identity, profile: dict[str, Any]) -> Launched:
    """启动（或复用）该身份的浏览器窗口。**必须有头**——无头与"真机真浏览器"定位冲突。"""
    from patchright.async_api import async_playwright

    if not config.has_display():
        raise RuntimeError(
            "本包要求有桌面会话（有头窗口）：无头模式在多站会被识别，且人工登录/过验证码必须有窗口")

    if ident.profile_mode == "user":
        # 共用用户日常浏览器的资料目录：直接用他已有的登录态（零扫码）
        d = user_profile_dir(ident.channel)
        if d is None:
            raise RuntimeError(
                "找不到你日常浏览器的资料目录（Edge 默认位置没找到）"
                "——可用环境变量 REAL_BROWSER_USER_DIR 显式指定")
        profile_dir = d
        lock = ProfileLock(config.home_dir() / f".lock-{ident.name}")   # 锁放我们这边，别写进他的目录
    else:
        profile_dir = Path(ident.profile_dir).expanduser()
        profile_dir.mkdir(parents=True, exist_ok=True)
        lock = ProfileLock(profile_dir / ".lock")
    lock.acquire()

    pw = await async_playwright().start()
    common: dict[str, Any] = dict(
        user_data_dir=str(profile_dir),
        channel=ident.channel,               # msedge | chrome（沿用账号历史，不为统一而换）
        headless=False,
        args=build_args(ident, profile),
        no_viewport=True,                    # 窗口尺寸由真实窗口决定，会话期不改
        ignore_default_args=["--no-sandbox"],
        # 注意：**不设 locale / timezone** —— 沿用系统真值，额外覆盖反而制造矛盾
    )
    if ident.browser_path:
        common.pop("channel")
        common["executable_path"] = ident.browser_path
    try:
        try:
            ctx = await pw.chromium.launch_persistent_context(**common)
        except Exception:
            # 极少数环境 channel 解析不到（如未安装对应浏览器）→ 给出可操作的错误
            raise RuntimeError(
                f"启动浏览器失败（channel={ident.channel}）——"
                f"确认本机装了 {'Microsoft Edge' if ident.channel == 'msedge' else 'Google Chrome'}，"
                f"或用 identity.browser_path 显式指定二进制") from None
    except Exception:
        await pw.stop()
        lock.release()
        raise

    # 把上次落盘的 cookie（**含会话级**）灌回去：Chromium 退出时会丢掉会话 cookie，
    # 而"扫码登录一次、以后都在"靠的就是它们（见 session.py 顶部说明）
    await load_cookies(ctx, ident.name)

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.bring_to_front()          # 置前台：真用户窗口不会被压在别的窗口后面
    except Exception:
        pass
    return Launched(pw=pw, ctx=ctx, page=page, lock=lock, channel=ident.channel)


async def close(l: Launched) -> None:
    """优雅关闭：让 Chromium 把 profile（cookie 等）正常落盘。"""
    for step in (l.ctx.close, l.pw.stop):
        try:
            await step()
        except Exception:
            pass
    l.lock.release()


async def resolve_final_url(url: str, *, channel: str = "msedge", settle: float = 6.0) -> str | None:
    """解析重定向链的最终 URL（patchright"加载中重定向丢上下文"的恢复通道）。

    做法：起一个**一次性裸浏览器**（临时 profile、仅本机端口、用完即杀、不带任何登录态）读真实 URL。
    背景与实测见 stealth-browser 方案 §5.6；本包沿用同一修法。
    """
    import json
    import shutil
    import subprocess
    import tempfile
    import urllib.parse
    import urllib.request

    exe = _system_browser_exe(channel)
    if not exe:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="ab-resolve-"))
    port = 9399
    proc = subprocess.Popen(
        [str(exe), "--headless", f"--user-data-dir={tmp}", f"--remote-debugging-port={port}",
         "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{base}/json/version", timeout=1).read()
                break
            except Exception:
                await asyncio.sleep(0.25)
        else:
            return None
        quoted = urllib.parse.quote(url, safe="")
        new = json.loads(urllib.request.urlopen(
            urllib.request.Request(f"{base}/json/new?{quoted}", method="PUT"), timeout=5).read())
        tid = new.get("id")
        for _ in range(int(settle * 4)):
            await asyncio.sleep(0.25)
            tabs = json.loads(urllib.request.urlopen(f"{base}/json/list", timeout=2).read())
            t = next((x for x in tabs if x.get("id") == tid), None)
            if t and t.get("url") and t["url"] != url and t.get("title"):
                return t["url"]
        return None
    except Exception:
        return None
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _system_browser_exe(channel: str) -> Path | None:
    """系统浏览器二进制定位（仅用于上面那个一次性解析器，不用于主会话）。"""
    import os
    if config.is_windows():
        cands = {
            "msedge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                       r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
            "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                       r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
        }.get(channel, [])
    elif config.is_macos():
        cands = {
            "msedge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
            "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        }.get(channel, [])
    else:
        cands = [p for p in (
            os.path.expanduser("~/.cache/ms-playwright/chromium/chrome-linux/chrome"),) ]
    for c in cands:
        p = Path(c)
        if p.is_file():
            return p
    return None


# ---------- 共用用户日常浏览器的资料目录（--profile user 模式） ----------
#
# 为什么这么设计（2026-09-24 与用户定）：专属 profile 的代价是"每次都要重新扫码"，
# 而共用用户自己的资料目录 = 用他已有的登录态（零扫码），也更贴近"直连用户日常浏览器"那条路线。
# 代价：Chromium **同一个资料目录同时只允许一个实例**（进程单例，OS 级文件锁），
# 所以这里**不硬抢**——检测到他的浏览器在跑就停手，把选择交给人（自己关 / 让 agent 帮关）。

def user_profile_dir(channel: str = "msedge") -> Path | None:
    """用户日常浏览器的资料目录（Windows 默认位置；可用 REAL_BROWSER_USER_DIR 覆盖）。"""
    from .identity import USER_DIR_ENV            # 放函数里，避免顶层循环导入
    env = os.environ.get(USER_DIR_ENV)
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    if os.name != "nt":
        return None
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    rel = Path("Microsoft/Edge/User") if channel == "msedge" else Path("Google/Chrome/User Data")
    p = Path(base) / rel
    return p if p.exists() else None


def user_browser_running(channel: str = "msedge") -> tuple[bool, str]:
    """用户的日常浏览器在不在跑？（在跑 = 资料目录被占着，我们拿不到）"""
    exe = "msedge.exe" if channel == "msedge" else "chrome.exe"
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=20).stdout
            n = sum(1 for ln in out.splitlines() if exe in ln.lower())
        else:
            pat = "Microsoft Edge" if channel == "msedge" else "Google Chrome"
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=10).stdout
            n = len(out.split())
    except Exception as e:                                 # noqa: BLE001
        return False, f"检测失败：{e}"
    return (True, f"{n} 个进程") if n else (False, "")


def close_user_browser(channel: str = "msedge", wait_sec: float = 12.0) -> tuple[bool, str]:
    """**优雅**关闭用户的日常浏览器（taskkill 不带 /F = 发关闭信号，不是强杀）。

    只在用户明确同意后调用——那是他的浏览器。关不掉（比如 Edge 弹了"关闭所有标签页？"）
    就如实回报，别升级成强杀。
    """
    exe = "msedge.exe" if channel == "msedge" else "chrome.exe"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/IM", exe], capture_output=True, text=True, timeout=25)
        else:
            pat = "Microsoft Edge" if channel == "msedge" else "Google Chrome"
            subprocess.run(["pkill", "-TERM", "-f", pat], timeout=15)
    except Exception as e:                                 # noqa: BLE001
        return False, f"关闭命令没执行成功：{e}"
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        running, _ = user_browser_running(channel)
        if not running:
            return True, "已关闭"
        time.sleep(0.8)
    return False, (f"{exe} 还没退（多半弹了“要关闭所有标签页吗”的确认框）"
                   f"——请在那台机器的浏览器窗口上点确认，或者手动关掉，然后再继续")
