"""配置与目录定位——跨平台（Windows / macOS / Linux 同级）。

原则：**不覆盖任何本机已有的东西**——身份越接近原生环境越稳定。
系统 locale / timezone / 字体 / GPU / 屏幕，一律由真实系统提供，本包不设、不改、不伪造。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP = "real-browser"
CONFIG_ENV = "REAL_BROWSER_CONFIG"
HOME_ENV = "REAL_BROWSER_HOME"


def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def user_config_dir() -> Path:
    """配置/身份目录：Windows → %APPDATA%；macOS → Application Support；其它 → ~/.config"""
    if is_windows():
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP
    return Path.home() / ".config" / APP


def data_home() -> Path:
    """运行时资产（profile/截图/下载/审计）：Windows → %LOCALAPPDATA%；macOS → Application Support；其它 → ~/.local/share"""
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP
    return Path.home() / ".local" / "share" / APP


def has_display() -> bool:
    """有没有可用桌面会话（本包**必须有头**：无头会在多站被识别，且与"真机真浏览器"定位冲突）。"""
    if is_windows() or is_macos():
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _candidates() -> list[Path]:
    out: list[Path] = []
    if os.environ.get(CONFIG_ENV):
        out.append(Path(os.environ[CONFIG_ENV]).expanduser())
    out.append(Path.cwd() / "real-browser.config.json")
    out.append(user_config_dir() / "config.json")
    return out


def load_config() -> dict:
    cfg: dict = {"home": os.environ.get(HOME_ENV) or str(data_home())}
    for p in _candidates():
        if p.is_file():
            cfg.update(json.loads(p.read_text("utf-8")))
            base = p.parent
            v = cfg.get("home")
            if v and not Path(os.path.expanduser(str(v))).is_absolute():
                cfg["home"] = str((base / os.path.expanduser(str(v))).resolve())
            break
    return cfg


def home_dir() -> Path:
    return Path(load_config()["home"]).expanduser()


# ---------- 收尾策略（2026-09-28）----------

IDLE_CLOSE_ENV = "REAL_BROWSER_IDLE_CLOSE_S"


def idle_close_s() -> int:
    """空闲多久自动关闭浏览器（秒）。默认 300（5 分钟）；0 = 不自动关。

    为什么要有它：驱动浏览器的是常驻守护进程（daemon.py），它的设计是"宿主回收也不关窗口"
    ——这对人机交接是对的，但**智能体跑完不调 browse_close 时窗口会一直留在用户桌面上**。
    本项是兜底：闲置超过该时长自动优雅关闭（登录态照样落盘），下次调用冷启动重开。
    """
    raw = (os.environ.get(IDLE_CLOSE_ENV) or "").strip()
    if raw.isdigit():
        return int(raw)
    return 300
