"""任务队列（三期 M19：Redis 分布式信号量，跨 worker 共享限流；Redis 不可用降级进程内 Semaphore）。

队列与并发配额（PRD v1.1-rev2 排队机制；GLM 官方并发配额低）：
- default: 5    （file_parse / file_search / web_search / memory）
- vision: 3     （image_recognition，GLM 视觉并发 5 留余量）
- image_gen: 2  （image_generation，CogView 并发 1-2）
- report: 2     （generate_chart / doc_export）
- video: 5      （媒体理解）
- sandbox: 2    （三期 M17 沙箱执行）

Redis 租约实现（dispatch 契约不变，tool_exec 零改动）：
- ZSET 成员 = 租约，score = 过期时间戳；acquire 用 Lua 原子（清理过期 + 计数 + 加租约）
- TTL = QUEUE_TIMEOUT + 30s（worker 崩溃时租约自动过期，防死锁）
- Redis 不可用 → 自动降级 asyncio.Semaphore（现状行为）
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Awaitable

from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger("agent.queue")

# 并发容量（2026-08-11 压测调优，30 人并发目标）：
# - 本地资源队列上调：default 15 / report 10（chart_png Semaphore 联动）/ sandbox 8 /
#   subagent 8 / video 8 / vision 3（实测 5 并发触发 GLM 1302 限流 → 3）
# - 外部 API 限流队列保持：image_gen 2（GLM cogview 官方并发 1）、video_gen 1（Agnes 1 RPM）
# read：只读类工具队列（2026-08-28 根因修复——未注册导致
# QUEUE_CONFIG[queue] KeyError('read') 被包装成 "redis 操作失败: 'read'" 掩盖）
QUEUE_CONFIG = {"read": 15, "default": 15, "vision": 3, "image_gen": 2, "report": 10, "video": 8, "sandbox": 8, "subagent": 8, "video_gen": 1,
                # 2026-09-15 智能助手媒体工具：转写并发 1——FunASR 全局串行锁是事实上的并发上限，
                # 队列 1 让"排队位置"如实反映等待（video_understand 走 video 队列）
                "transcribe": 1}
QUEUE_TIMEOUT = {"read": 300, "default": 300, "vision": 300, "image_gen": 600, "report": 600, "video": 900, "sandbox": 60, "subagent": 600, "video_gen": 900,
                 # 2026-09-15：transcribe 900s（2h 音频推理约 4-5 分钟 + 全局锁排队余量）；
                 # video 600→900（video_understand = 转写 + 分段视觉，20 分钟视频分钟级耗时 + 排队）
                 "transcribe": 900}

# Lua 原子获取租约：清理过期 → 计数 → 未满则 ZADD（返回新位置）→ 满返回 0
_ACQUIRE_LUA = """
local key = KEYS[1]
redis.call('ZREMRANGEBYSCORE', key, 0, ARGV[2])
local cnt = redis.call('ZCARD', key)
if cnt < tonumber(ARGV[1]) then
  redis.call('ZADD', key, ARGV[2] + tonumber(ARGV[3]), ARGV[4])
  return cnt + 1
end
return 0
"""


class QueueManager:
    def __init__(self):
        self._sems: dict[str, asyncio.Semaphore] = {
            q: asyncio.Semaphore(n) for q, n in QUEUE_CONFIG.items()
        }
        # S10：get_redis 惰性建连从不返回 None → _redis_mode 恒 True（原"降级"永不发生）。
        # 改为乐观 Redis 模式 + 首次故障即降级（_degraded 标记 + 日志），降级后走进程内 Semaphore。
        self._redis_mode = True
        self._degraded = False

    def _degrade(self, reason: str) -> None:
        if not self._degraded:
            self._degraded = True
            logger.warning("队列降级为进程内 Semaphore（Redis 不可用）: %s", reason)

    def _lease_key(self, queue: str) -> str:
        return f"qm:lease:{queue}"

    async def queue_position(self, queue: str) -> int:
        """当前排队位置（含执行中租约数；Redis 不可用时进程内等待数）。"""
        if self._redis_mode:
            r = get_redis()
            if r is not None:
                try:
                    now = time.time()
                    await r.zremrangebyscore(self._lease_key(queue), 0, now)
                    return await r.zcard(self._lease_key(queue))
                except Exception:
                    pass
        sem = self._sems[queue]
        waiters = getattr(sem, "_waiters", None)
        return len(waiters) if waiters else 0

    async def _redis_acquire(self, queue: str, member: str) -> int:
        """尝试获取 Redis 租约（member 由调用方生成以便释放）；0=已满（排队）。

        S10：Redis 故障抛 ConnectionError（原吞异常返回 0 → 轮询至 E005，降级永不触发）。
        """
        r = get_redis()
        if r is None:
            raise ConnectionError("redis 连接不可用")
        try:
            pos = await r.eval(
                _ACQUIRE_LUA,
                1,
                self._lease_key(queue),
                QUEUE_CONFIG[queue],
                time.time(),
                QUEUE_TIMEOUT[queue] + 30,
                member,
            )
            return int(pos) if pos else 0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("队列 Redis eval 失败（完整栈）: queue=%s", queue)  # 根因彻查临时日志
            raise ConnectionError(f"redis 操作失败: {str(e)[:100]}") from e

    async def _redis_release(self, queue: str, member: str) -> None:
        r = get_redis()
        if r is None:
            return
        try:
            await r.zrem(self._lease_key(queue), member)
        except Exception:
            pass

    async def dispatch(self, queue: str, coro_factory) -> dict:
        """带限流与超时执行，返回 {queued, position, duration_s, result}。

        queued=True 表示进入过等待队列；超时抛出 asyncio.TimeoutError（由上层转为 E005）。
        E-01（2026-08-10）：coro_factory 每次调用返回**新建** coroutine——
        原实现收已 await 过的 coroutine，工具自身异常被 Redis 故障分支捕获后二次 await →
        RuntimeError: cannot reuse already awaited coroutine（真实错误被掩盖）；
        且降级仅在 ConnectionError（Redis 故障）时触发，工具真实异常直接上抛。
        """
        started = time.time()
        if self._redis_mode and not self._degraded:
            try:
                return await self._dispatch_redis(queue, coro_factory, started)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                raise
            except ConnectionError as e:  # 仅 Redis 故障降级（工具真实异常不再误判为降级）
                self._degrade(str(e)[:120])
        return await self._dispatch_local(queue, coro_factory, started)

    async def _dispatch_redis(self, queue: str, coro_factory, started: float) -> dict:
        """Redis 租约版：获取租约（满则轮询排队，超时抛 TimeoutError）→ 执行 → 释放。"""
        r = get_redis()
        member = f"{uuid.uuid4().hex}"
        pos = await self._redis_acquire(queue, member)
        queued = pos == 0
        if queued:
            # 排队等待槽位（轮询 0.5s；取消传播；排队总时长超 QUEUE_TIMEOUT 抛 E005）
            waited = 0.0
            while True:
                pos = await self._redis_acquire(queue, member)
                if pos:
                    break
                await asyncio.sleep(0.5)
                waited += 0.5
                if waited > QUEUE_TIMEOUT[queue]:
                    raise asyncio.TimeoutError("队列等待超时")
        position = pos - 1
        try:
            result = await asyncio.wait_for(coro_factory(), timeout=QUEUE_TIMEOUT[queue])
        finally:
            await self._redis_release(queue, member)
        return {"queued": queued, "position": position, "duration_s": round(time.time() - started, 2), "result": result}

    async def _dispatch_local(self, queue: str, coro_factory, started: float) -> dict:
        """进程内 Semaphore 版（Redis 不可用兜底，一期行为）。"""
        sem = self._sems[queue]
        position = await self.queue_position(queue)   # 2026-09-10：漏 await（同类排查）——原拿到的是协程对象
        async with sem:
            result = await asyncio.wait_for(coro_factory(), timeout=QUEUE_TIMEOUT[queue])
        return {"queued": position > 0, "position": position, "duration_s": round(time.time() - started, 2), "result": result}


# 进程级单例
_qm: QueueManager | None = None


def get_queue_manager() -> QueueManager:
    global _qm
    if _qm is None:
        _qm = QueueManager()
    return _qm
