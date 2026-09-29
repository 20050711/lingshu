"""生成类工具的「重复触发防护」（2026-09-17，缓存审计 P2-12）。

生成结果本身不确定，**不适合做结果缓存**（用户要的就是每次不一样）；这里防的是
「同一参数在几分钟内被重复触发」——典型场景：网关/审核失败后 agent 原样重试、
或同一个 prompt 在相邻两轮被再调一次。图片是付费调用，视频还有每日秒数配额
（`agnes_video_daily_seconds`），重复一次都是真花钱。

命中时**不报错、也不硬拦**：把上次产物原样返回并附一句说明，agent 据此告诉用户
"刚生成过、直接给你上次那张"；用户确实要再来一张/一段时，显式传 `regenerate=true` 绕过。

键含 user_id（不同人同 prompt 互不影响）；TTL 10 分钟（见 `TTL_S`）。
"""
from __future__ import annotations

import hashlib
import json
import time

from app.core.redis import redis_get, redis_set

TTL_S = 600


def gen_key(kind: str, user_id: int, sig: dict) -> str:
    body = json.dumps(sig, ensure_ascii=False, sort_keys=True, default=str)
    return f"gen_dedupe:{kind}:u{user_id}:{hashlib.md5(body.encode()).hexdigest()}"


async def recent(kind: str, user_id: int, sig: dict) -> dict | None:
    """取最近一次同参数生成的产物（无则 None）。返回里含 `_ago_min` 供文案用。"""
    try:
        raw = await redis_get(gen_key(kind, user_id, sig))
        if not raw:
            return None
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    at = float(data.pop("_at", 0) or 0)
    data["_ago_min"] = max(0, int((time.time() - at) / 60)) if at else 0
    return data


async def remember(kind: str, user_id: int, sig: dict, payload: dict) -> None:
    """记一次生成结果（失败静默——防护不该影响主流程）。"""
    try:
        await redis_set(gen_key(kind, user_id, sig),
                        json.dumps({**payload, "_at": time.time()}, ensure_ascii=False), TTL_S)
    except Exception:
        pass
