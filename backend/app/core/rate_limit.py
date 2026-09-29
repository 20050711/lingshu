"""通用速率限制（SEC-05，2026-08-17）：基于 Redis 计数（内存降级），FastAPI 依赖工厂。

- 复用 redis.redis_incr 计数基座（Redis 不可用时降级内存 dict，单 worker 等价）
- by="user"：按用户 ID（需已鉴权接口，如 /chat/ask）；by="ip"：按客户端 IP（未鉴权接口，
  IP 提取走 core.security.get_client_ip——SEC-07 修正版，仅可信反代场景信任 XFF）
- 超限抛 E005（429，错误码已注册）；限流在请求进入时一次判定（SSE 长连接只计数一次）
"""
from __future__ import annotations

from fastapi import Depends, Request

from app.core.exceptions import app_error
from app.core.middleware import get_current_user
from app.core.redis import redis_incr
from app.core.security import get_client_ip


async def check_rate(key: str, limit: int, window: int = 60) -> bool:
    """计数 +1，返回是否超限（超限本身也计数，但仅判断 > limit）。"""
    n = int(await redis_incr(key, window) or 0)
    return n > limit


def rate_limit_dep(prefix: str, limit: int, window: int = 60, by: str = "user"):
    """FastAPI 依赖工厂。by="user" 按用户 ID（需已鉴权）；by="ip" 按客户端 IP（未鉴权接口用）。"""
    if by == "user":

        async def _dep(request: Request, user: dict = Depends(get_current_user)) -> None:
            ident = str(user["user_id"])
            if await check_rate(f"rl:{prefix}:{ident}", limit, window):
                raise app_error("E005", "请求过于频繁，请稍后再试", status_code=429)

    else:

        async def _dep(request: Request) -> None:
            ident = get_client_ip(request)
            if await check_rate(f"rl:{prefix}:{ident}", limit, window):
                raise app_error("E005", "请求过于频繁，请稍后再试", status_code=429)

    return _dep
