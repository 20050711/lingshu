"""外部连通性探针（2026-09-11 走查后加固，用户拍板"两边都做"）。

背景：2026-09-11 开发机后端进程出现"对外请求集体超时、同机新进程正常"的故障（`httpx.ConnectTimeout`：
LLM 调 DeepSeek/agnes 150s 超时、代理池拉源全部失败），用户侧只感觉"卡"，运维无从感知；根因未抓到
（见 `docs/交接文档/HANDOVER.md` 踩坑 43）。

本探针每 5 分钟对**配置里的 LLM 端点**（deepseek / agnes，取自 settings，不硬编码）做一次
**TCP + TLS 握手**——不发任何 API 请求、零费用、不计配额：

- 全部端点失败 → WARNING（每次）+ 告警推送（连续 ≥2 次触发，之后每小时复报；恢复即清零）
- 告警文案直接带上处置命令（重启后端），运维照做即可恢复
- 只做"能自愈的动作"：目前自有 HTTP 客户端均为**按次创建**（无长驻连接池可重建），
  故不做假自愈；真正的恢复手段=重启后端（进程级状态刷新）
"""
from __future__ import annotations

import asyncio
import ssl
import time
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("services.net_probe")

_settings = get_settings()

_PROBE_TIMEOUT = 5.0        # 单端点握手超时（好网络 <1s）
_FAIL_THRESHOLD = 2         # 连续失败几次触发告警（5 分钟/次 → 约 10 分钟）
_REALERT_S = 3600.0         # 故障持续时每小时复报一次（防刷屏）

_fail_streak = 0
_last_alert_at = 0.0
_seen_ok = False     # 首轮成功打一条"就绪"日志（成功常态静默，便于部署后确认探针在跑）


async def _probe_one(url: str, timeout: float = _PROBE_TIMEOUT) -> tuple[bool, str]:
    """对 url 的 host:port 做 TCP(+TLS) 握手；返回 (是否连通, 说明)。不发起 HTTP 请求。"""
    u = urlparse(url)
    host = u.hostname
    if not host:
        return False, "端点未配置"
    use_tls = (u.scheme or "").lower() == "https"
    port = u.port or (443 if use_tls else 80)
    try:
        ctx = ssl.create_default_context() if use_tls else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if use_tls else None),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _targets() -> list[tuple[str, str]]:
    """探测目标 = 配置的文本模型端点（去重）。"""
    out: list[tuple[str, str]] = []
    for name, url in (("deepseek", _settings.deepseek_base_url),
                      ("agnes", _settings.agnes_base_url)):
        if url and url not in [u for _, u in out]:
            out.append((name, url))
    return out


async def probe_external() -> dict:
    """探测全部配置端点：{"ok": 任一通, "all_down": 全不通, "summary": "deepseek=ok; agnes=超时"}。"""
    targets = _targets()
    if not targets:
        return {"ok": True, "all_down": False, "summary": "无配置端点（跳过）", "results": []}
    results = await asyncio.gather(*(_probe_one(u) for _, u in targets))
    parts = [f"{name}={'ok' if ok else why}" for (name, _), (ok, why) in zip(targets, results)]
    return {"ok": any(ok for ok, _ in results),
            "all_down": not any(ok for ok, _ in results),
            "summary": "; ".join(parts),
            "results": [{"name": n, "ok": ok, "detail": why} for (n, _), (ok, why) in zip(targets, results)]}


async def run_probe_job() -> None:
    """定时任务体（调度器每 5 分钟调一次）。全端点不可达 → 计数/告警；恢复 → 清零。"""
    global _fail_streak, _last_alert_at, _seen_ok
    r = await probe_external()
    if not r["all_down"]:
        if _fail_streak:
            logger.info("外部连通性已恢复（此前连续失败 %d 次）：%s", _fail_streak, r["summary"])
        elif not _seen_ok:
            logger.info("外部连通性探针就绪：%s", r["summary"])
        _seen_ok = True
        _fail_streak = 0
        return
    _fail_streak += 1
    logger.warning("外部连通性探测失败（连续 %d 次）：%s", _fail_streak, r["summary"])
    now = time.time()
    if _fail_streak < _FAIL_THRESHOLD:
        return
    if _last_alert_at and now - _last_alert_at < _REALERT_S:
        return
    _last_alert_at = now
    try:
        from app.services.alert import notify

        await notify(
            "外部网络异常（LLM 端点不可达）",
            f"连续 {_fail_streak} 次探测失败：{r['summary']}\n"
            "典型表现：问答变慢/报「LLM 调用失败」、代理池拉源失败（长跑进程对外连接异常）。\n"
            "处置：重启后端 —— `bash deploy/stop.sh --backend-only && bash deploy/start.sh`"
            "（开发机：重起 uvicorn）。",
        )
    except Exception as e:   # 告警通道本身失败不能影响主流程
        logger.warning("连通性告警发送失败：%s", str(e)[:120])
