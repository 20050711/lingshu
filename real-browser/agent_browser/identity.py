"""身份：一账号 = 一 profile + 一浏览器（**真机真浏览器，无指纹参数**）。

身份是**状态**不是配置：默认落在 ``<config>/identities.json``，不入库。
与 stealth-browser 的区别：那边身份 = seed 画像；这边身份 = 机器上的一个 profile 目录 + 浏览器家族。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from . import config

MODE_ENV = "REAL_BROWSER_PROFILE"          # dedicated（默认）| user
USER_DIR_ENV = "REAL_BROWSER_USER_DIR"     # 可显式指定用户浏览器的资料目录


def profile_mode() -> str:
    """资料目录模式：``dedicated`` = 本包专属 profile（默认，互不干扰）；
    ``user`` = 直接共用用户日常浏览器的资料目录（**不用再扫码**，但用工具期间他的浏览器得让位）。"""
    return "user" if (os.environ.get(MODE_ENV) or "").strip().lower() == "user" else "dedicated"


@dataclass
class Identity:
    name: str
    site: str = "default"
    channel: str = "msedge"          # msedge | chrome（跟随账号历史，不要为统一而换）
    browser_path: str | None = None  # 显式指定二进制（一般不用，channel 会自动定位）
    proxy: str | None = None
    profile_dir: str = ""
    profile_mode: str = "dedicated"  # 见 profile_mode()；由环境变量决定，重启后仍生效
    login_state: str = "unknown"     # ok | not_logged_in | check_failed | unknown
    login_checked_at: float = 0.0
    cooldown_until: float = 0.0      # 站点信号触发的冷却（guard 维护）
    cooldown_reason: str = ""

    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until


def identities_path():
    return config.user_config_dir() / "identities.json"


def _load_all() -> dict:
    p = identities_path()
    if p.is_file():
        return json.loads(p.read_text("utf-8"))
    return {}


def load_identity(name: str, site: str | None = None, channel: str | None = None) -> Identity:
    """取身份；不存在则新建并落盘（profile 目录按名字生成）。"""
    raw = _load_all()
    if name in raw:
        ident = Identity(**raw[name])
        if site:
            ident.site = site
        if channel:
            ident.channel = channel
        ident.profile_mode = profile_mode()      # 环境变量说话（接入脚本写进 mcp.json 的 env）
        return ident
    home = config.home_dir()
    ident = Identity(name=name, site=site or "default", channel=channel or "msedge",
                     profile_dir=str(home / "profiles" / name), profile_mode=profile_mode())
    save_identity(ident)
    return ident


def save_identity(ident: Identity) -> None:
    p = identities_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = _load_all()
    raw[ident.name] = asdict(ident)
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
