"""Redis 连接（验证码/登录失败计数/IP 黑名单/Token 黑名单）。

Redis 不可用时降级为内存 dict（开发容错），接口不变。
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis

from app.core.config import get_settings

_settings = get_settings()

_client: aioredis.Redis | None = None
_memory: dict[str, tuple[float, Any]] = {}   # (expire_at, value) 降级存储
_lists: dict[str, list[str]] = {}            # 降级列表存储（中断队列 fallback，单 worker 语义等价）
_lock = asyncio.Lock()


def get_redis() -> aioredis.Redis | None:
    global _client
    if _client is None:
        try:
            # SEC-18：支持 REDIS_PASSWORD（redis.conf requirepass 场景；未配置透传 None 行为不变）
            # 2026-08-19（S1-2）：socket_timeout/connect_timeout 必设——Redis 连接半开时
            # rpush 曾永久挂起 → relay 单消费者卡死 → plan/question 事件丢失（300s 超时根因之一）
            _client = aioredis.from_url(
                _settings.redis_url,
                password=_settings.redis_password or None,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
        except Exception:
            _client = None
    return _client


async def redis_ping() -> bool:
    """真实探活（M5：健康检查不再 or True 恒真）。"""
    r = get_redis()
    if r is None:
        return False
    try:
        await r.ping()
        return True
    except Exception:
        return False


async def redis_scan_delete(pattern: str) -> int:
    """按模式扫描并删除键（M6：DEL 不支持通配符，cache_clear 用）。返回删除数。"""
    r = get_redis()
    if r is None:
        return 0
    deleted = 0
    try:
        async for key in r.scan_iter(match=pattern, count=500):
            await r.delete(key)
            deleted += 1
    except Exception:
        pass
    return deleted


async def redis_set(key: str, value: str, ttl: int) -> None:
    r = get_redis()
    if r is not None:
        try:
            await r.set(key, value, ex=ttl)
            return
        except Exception:
            pass
    _memory[key] = (asyncio.get_event_loop().time() + ttl, value)


async def redis_get(key: str) -> str | None:
    r = get_redis()
    if r is not None:
        try:
            return await r.get(key)
        except Exception:
            pass
    item = _memory.get(key)
    if item is None:
        return None
    expire_at, value = item
    if asyncio.get_event_loop().time() > expire_at:
        _memory.pop(key, None)
        return None
    return value


async def redis_incr(key: str, ttl: int) -> int:
    r = get_redis()
    if r is not None:
        try:
            val = await r.incr(key)
            if val == 1:
                await r.expire(key, ttl)
            return int(val)
        except Exception:
            pass
    cur = int(await redis_get(key) or 0) + 1
    await redis_set(key, str(cur), ttl)
    return cur


async def redis_delete(*keys: str) -> None:
    r = get_redis()
    if r is not None:
        try:
            await r.delete(*keys)
            return
        except Exception:
            pass
    for k in keys:
        _memory.pop(k, None)


# v2（2026-08-14）：中断队列（interrupt:{session} 列表结构，LPOP 全量+DEL 消费）
async def redis_rpush(key: str, value: str) -> None:
    r = get_redis()
    if r is not None:
        try:
            await r.rpush(key, value)
            return
        except Exception:
            pass
    _lists.setdefault(key, []).append(value)


# 2026-08-20（P1-③）：原子消费脚本——原 lrange/delete 两步之间 rpush 的条目会被 delete
# 误删（子代理完成通知与用户插话共用队列，队列非空时窗口真实存在）
_LPOP_ALL_LUA = (
    "local items = redis.call('LRANGE', KEYS[1], 0, -1)\n"
    "redis.call('DEL', KEYS[1])\n"
    "return items"
)


async def redis_lpop_all(key: str) -> list[str]:
    """原子取出列表全部元素并删除键（Lua 单次执行；socket_timeout=5 保护）。返回 [] 表示无内容。"""
    r = get_redis()
    if r is not None:
        try:
            return list(await r.eval(_LPOP_ALL_LUA, 1, key)) or []
        except Exception:
            pass
    return _lists.pop(key, [])


# 2026-08-18（会话任务后台化）：事件流读取/续命——seq = Redis list index
async def redis_lrange(key: str, start: int, end: int) -> list[str]:
    """按索引范围取列表元素（含两端；end=-1 到末尾）。事件流重连回放用。"""
    r = get_redis()
    if r is not None:
        try:
            return list(await r.lrange(key, start, end))
        except Exception:
            pass
    lst = _lists.get(key, [])
    if end == -1:
        end = len(lst) - 1
    return lst[start:end + 1] if start >= 0 and end >= start else []


async def redis_llen(key: str) -> int:
    r = get_redis()
    if r is not None:
        try:
            return int(await r.llen(key))
        except Exception:
            pass
    return len(_lists.get(key, []))


async def redis_expire(key: str, ttl: int) -> None:
    """刷新键 TTL（事件流滑动过期）。内存降级列表无 TTL——单 worker 会话级数据，可接受。"""
    r = get_redis()
    if r is not None:
        try:
            await r.expire(key, ttl)
            return
        except Exception:
            pass
