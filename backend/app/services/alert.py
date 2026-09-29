"""告警推送：机器人 Webhook（富文本 post 消息；URL 由 ALERT_WEBHOOK_URL 配置）。

- 富文本消息（msg_type=post，文档"发送富文本消息"）：标题 + 段落，段落内 text 支持
  加粗 等富文本语法；不再使用纯文本（用户要求按文档写非单文本样式）。
- 签名校验模式（HmacSHA256(timestamp\n密钥) → Base64）：机器人开启"签名"安全设置时须带
  timestamp 与 sign 参数（FEISHU_SIGN_SECRET 配置后自动附加；未配置则裸 URL 推送）。
- 限流：机器人侧 100 次/分、5 次/秒 → 本地节流信号量 5/s + 内容截断 1500 字。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("services.alert")
_settings = get_settings()

# 节流：本地信号量 5/s（机器人限 100 次/分、5 次/秒；推送不阻塞主流程）
_throttle = asyncio.Semaphore(5)
_last_second: list[float] = []


def _sign_params(url: str) -> str:
    """告警机器人开启签名时附加 timestamp/sign（HmacSHA256(timestamp\n密钥) → Base64 URL 安全）。"""
    secret = getattr(_settings, "alert_sign_secret", "")
    if not secret:
        return url
    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    sign = base64.b64encode(digest).decode()
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote(sign, safe='')}"


async def notify(title: str, content: str = "", paragraphs: list | None = None) -> bool:
    """推送富文本消息（msg_type=post，标题加粗展示）。

    paragraphs: post 段落列表 [[{tag,text},...], ...]，text 支持 加粗 富文本语法；
    缺省时用 content 组成单段落（内容截断 1500 字）。webhook 未配置或失败时静默。
    """
    # 2026-08-26：开发机不推告警（start.sh 注入 env_name=开发机；部署机为空不受影响）
    if _settings.env_name == "开发机":
        return False
    url = _settings.alert_webhook_url
    if not url:
        logger.info("告警跳过（未配置 webhook）: %s", title)
        return False
    # 2026-08-21：环境标识前缀（env_name 非空时 [开发机] 等——区分多环境告警来源）
    if _settings.env_name:
        title = f"[{_settings.env_name}] {title}"
    try:
        # 本地节流 5/s：等待不超过 1s，超时则跳过本次（Semaphore 无超时可能挂起调用方）
        try:
            await asyncio.wait_for(_throttle.acquire(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("告警节流：获取信号量超时，跳过本次 %s", title)
            return False
        try:
            _last_second.append(time.time())
            recent = [t for t in _last_second if time.time() - t < 1.0]
            _last_second[:] = recent
            if len(recent) > 5:
                logger.warning("告警节流：1 秒内推送超 5 条，跳过本次 %s", title)
                return False
            # 自定义机器人富文本消息（msg_type=post）
            body = paragraphs if paragraphs else [[{"tag": "text", "text": content[:1500]}]]
            payload = {
                "msg_type": "post",
                "content": {"post": {"zh_cn": {"title": title[:50], "content": body}}},
            }
            async with httpx.AsyncClient(timeout=10) as c:
                resp = await c.post(_sign_params(url), json=payload)
        finally:
            _throttle.release()
        ok = resp.status_code == 200 and resp.json().get("code") == 0
        if not ok:
            logger.warning("告警推送失败 status=%s body=%s", resp.status_code, resp.text[:200])
        return ok
    except Exception as e:
        logger.warning("告警推送异常: %s", str(e)[:200])
        return False
