"""护栏：URL 闸（SSRF）+ 节奏 + 审计 + 冷却 + 判死。

设计口径：**护栏挂在工具上，agent 绕不过**；被拒绝时抛 GuardError，
由 MCP 层翻译成"为什么 + 怎么办"的人话（不是堆栈）。

**分工（2026-09-24 定）**：
- 工具兜住"**想快也快不了**"——导航间隔在这，站点判死信号（URL 命中档案的 bad_url_patterns）
  也在（`BrowserAgent._canary_hit`）；
- 工具**不设"每小时多少次"的配额**——那是"规模"问题，硬拦会误伤正常使用
  （撞上限就得干等一小时），而站点真正的硬信号是它自己的额度提示。
  规模约束写进提示词（`pack/skill/SKILL.md` 的「节奏与规模」），由 agent 自觉遵守。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .identity import Identity, save_identity

ALLOWED_SCHEMES = ("http", "https")


class GuardError(Exception):
    """护栏拒绝（消息面向用户/agent 可读）。"""


# ---------- URL 闸 ----------

def check_url(url: str, *, allow_local: bool = False) -> str:
    u = urlparse(url or "")
    if u.scheme not in ALLOWED_SCHEMES:
        raise GuardError(f"只允许 http/https 网址（收到 {u.scheme or '空'}://）——"
                         f"file:/chrome:/about: 一律禁止")
    host = u.hostname
    if not host:
        raise GuardError("网址缺少主机名")
    if not allow_local and _blocked(host):
        raise GuardError(f"拒绝访问内网/回环地址：{host}（SSRF 防护；本地测试请显式开 allow_local）")
    return url


def _blocked(host: str) -> bool:
    try:
        return _ip_blocked(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # 解析不了就放行——浏览器自己也会报错，不构成 SSRF
    return any(_ip_blocked(ipaddress.ip_address(i[4][0])) for i in infos)


def _ip_blocked(ip) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


# ---------- 节奏 / 频控 ----------

class Pace:
    """节奏：导航之间按档案强加随机间隔。

    **只做"间隔"，不做"配额"**（见模块开头的分工说明）：间隔保证"想快也快不了"，
    但"做多少"交给提示词——工具不拦总量，也就不会把正常使用拦死。
    """

    def __init__(self, profile: dict) -> None:
        gap = (profile.get("pace") or {}).get("nav_gap_sec") or [0, 0]
        self.nav_gap = (float(gap[0]), float(gap[1]))
        self._last_nav = 0.0

    async def before_nav(self) -> None:
        lo, hi = self.nav_gap
        if lo > 0 and self._last_nav:
            wait = self._last_nav + random.uniform(lo, hi) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_nav = time.time()


# ---------- 审计 ----------

class Audit:
    def __init__(self, identity: str) -> None:
        self.dir = config.home_dir() / "audit"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{identity}.jsonl"

    def log(self, action: str, **kw) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, **kw}
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 审计失败不阻断业务


# ---------- 编排 ----------

class Guard:
    def __init__(self, profile: dict, identity: Identity, *, allow_local: bool = False) -> None:
        self.profile = profile
        self.identity = identity
        self.allow_local = allow_local
        self.audit = Audit(identity.name)
        self.pace = Pace(profile)

    def ensure_ready(self) -> None:
        if self.identity.in_cooldown():
            remain = int(self.identity.cooldown_until - time.time())
            why = self.identity.cooldown_reason or "站点信号"
            raise GuardError(f"该身份处于冷却中（剩 {remain}s）——原因：{why}")

    def cool_down(self, seconds: int, reason: str) -> None:
        self.identity.cooldown_until = time.time() + seconds
        self.identity.cooldown_reason = reason
        save_identity(self.identity)
        self.audit.log("cooldown", sec=seconds, reason=reason)

    async def before_nav(self, url: str) -> None:
        self.ensure_ready()
        check_url(url, allow_local=self.allow_local)
        await self.pace.before_nav()          # 只压间隔，不设每小时配额（见模块开头）

    def before_action(self, name: str, **kw) -> None:
        self.ensure_ready()
        self.audit.log(name, **kw)
