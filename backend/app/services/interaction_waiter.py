"""交互等待器：asyncio.Future 注册表（v2 框架，取代 confirm_waiter）。

- QuestionWaiter：反问浮窗等待（120s 超时自动按推荐项提交——agent 不悬挂）
- PlanWaiter：计划批准卡等待（900s 超时返回 timeout，收尾后卡片可持久化重开）

graph 的 ask_question / plan_approval 节点 await wait()，前端 POST /chat/answer、
/chat/plan-approve 调用 answer()/approve() 唤醒。单 worker 内存态即可；若将来
多 worker，替换为 Redis pub/sub（接口已隔离，与旧 ConfirmWaiter 同一约束）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings

_settings = get_settings()


class QuestionWaiter:
    def __init__(self, timeout_seconds: int | None = None):
        self.timeout_seconds = timeout_seconds or _settings.question_timeout_seconds
        self._waiters: dict[str, asyncio.Future] = {}

    @staticmethod
    def _key(session_id: str, question_id: str) -> str:
        return f"{session_id}:{question_id}"

    @staticmethod
    def new_question_id() -> str:
        return f"q_{uuid.uuid4().hex[:12]}"

    async def wait(self, session_id: str, question_id: str, default_answers: list[dict], extra_text: str = "") -> dict:
        """等待用户回答。超时自动返回推荐项（default_answers 由生成端按 recommended 构造）。

        返回 {"answers": [...], "extra_text": str, "source": "user"|"timeout"}。
        """
        key = self._key(session_id, question_id)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._waiters[key] = fut
        try:
            result = await asyncio.wait_for(fut, timeout=self.timeout_seconds)
            result["source"] = "user"
            return result
        except asyncio.TimeoutError:
            return {"answers": default_answers, "extra_text": extra_text, "source": "timeout"}
        finally:
            self._waiters.pop(key, None)

    def answer(self, session_id: str, question_id: str, answers: list[dict], extra_text: str = "") -> bool:
        """前端回答：唤醒等待中的反问。返回是否成功唤醒。"""
        key = self._key(session_id, question_id)
        fut = self._waiters.get(key)
        if fut is not None and not fut.done():
            fut.set_result({"answers": answers, "extra_text": extra_text})
            return True
        return False

    def expires_at(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.timeout_seconds)).isoformat()


class PlanWaiter:
    def __init__(self, timeout_seconds: int | None = None):
        self.timeout_seconds = timeout_seconds or _settings.plan_approve_timeout_seconds
        self._waiters: dict[str, asyncio.Future] = {}

    @staticmethod
    def _key(session_id: str, plan_id: str) -> str:
        return f"{session_id}:{plan_id}"

    @staticmethod
    def new_plan_id() -> str:
        return f"pl_{uuid.uuid4().hex[:12]}"

    async def wait(self, session_id: str, plan_id: str) -> tuple[str, str]:
        """等待批准，返回 (decision, feedback)。decision: approved/rejected/timeout。"""
        from app.core.logging import get_logger as _gl

        _logger = _gl("interaction.plan_waiter")
        key = self._key(session_id, plan_id)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._waiters[key] = fut
        _logger.info("PlanWaiter.wait 注册 key=%s timeout=%s", key, self.timeout_seconds)
        try:
            result = await asyncio.wait_for(fut, timeout=self.timeout_seconds)
            decision, feedback = result
            _logger.info("PlanWaiter.wait 被唤醒 decision=%s", decision)
            return decision, feedback
        except asyncio.TimeoutError:
            _logger.info("PlanWaiter.wait 超时 key=%s", key)
            return "timeout", ""
        finally:
            self._waiters.pop(key, None)

    def approve(self, session_id: str, plan_id: str, decision: str, feedback: str = "") -> bool:
        """前端批准/拒绝（拒绝可带必填意见）。返回是否成功唤醒。"""
        key = self._key(session_id, plan_id)
        fut = self._waiters.get(key)
        if fut is not None and not fut.done():
            fut.set_result((decision, feedback))
            return True
        return False

    def expires_at(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.timeout_seconds)).isoformat()


# 进程级单例
_question_waiter: QuestionWaiter | None = None
_plan_waiter: PlanWaiter | None = None


def get_question_waiter() -> QuestionWaiter:
    global _question_waiter
    if _question_waiter is None:
        _question_waiter = QuestionWaiter()
    return _question_waiter


def get_plan_waiter() -> PlanWaiter:
    global _plan_waiter
    if _plan_waiter is None:
        _plan_waiter = PlanWaiter()
    return _plan_waiter
