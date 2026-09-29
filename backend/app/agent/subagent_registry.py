"""后台子代理注册表（v2 框架，2026-08-14）。

真后台化：子代理以独立 asyncio.Task 运行，不阻塞主循环；完成时写回报告 +
推 interrupt 通知（主 agent 下一回合经中断队列消费）。主 agent 可经 resume_id
续聊已完成的子代理（保留其独立上下文 sub_msgs）。

重启语义：注册表为进程内存态（单 worker 部署）——uvicorn 重启后任务与上下文丢失，
resume_id 查询失败返回明确错误，主 agent 重派即可（设计书"重跑即可"）。
TTL 惰性清理：agent_llm 每次调用顺带 evict 过期任务，防泄漏。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("agent.subagent_registry")
_settings = get_settings()


@dataclass
class SubagentTask:
    id: str
    session_id: str
    role: str                       # generic / explore / plan / execute
    status: str = "running"         # running / done / failed / cancelled
    sub_msgs: list[dict] = field(default_factory=list)   # 独立上下文（续聊保留）
    work_subdir: str = ""
    task_text: str = ""             # 最近一次任务书（续聊前可见）
    started_at: float = 0.0
    finished_at: float | None = None
    report: str = ""
    error: str | None = None
    rounds_used: int = 0
    task: asyncio.Task | None = None


class SubagentRegistry:
    def __init__(self, ttl: int | None = None):
        self.ttl = ttl or _settings.subagent_registry_ttl
        self._tasks: dict[str, SubagentTask] = {}

    def register(self, t: SubagentTask) -> None:
        """登记一个后台子代理任务（task 字段已由调用方 create_task）。"""
        self._tasks[t.id] = t
        logger.info("后台子代理登记 sub=%s role=%s session=%s", t.id, t.role, str(t.session_id)[:8])

    def get(self, sub_id: str) -> SubagentTask | None:
        t = self._tasks.get(sub_id)
        if t is None:
            return None
        if t.finished_at and time.monotonic() - t.finished_at > self.ttl:
            self._tasks.pop(sub_id, None)
            return None
        return t

    def evict_expired(self) -> int:
        """TTL 惰性清理（agent_llm 每次调用顺带执行）。"""
        now = time.monotonic()
        expired = [k for k, t in self._tasks.items()
                   if t.finished_at and now - t.finished_at > self.ttl]
        for k in expired:
            self._tasks.pop(k, None)
        if expired:
            logger.info("子代理注册表清理 %d 个过期任务", len(expired))
        return len(expired)

    async def wait_all(self, ids: list[str], timeout: float = 600.0) -> list[dict]:
        """阻塞汇合一批后台子代理。超时后仍在跑的任务返回 running 状态（不取消）。

        返回每个 id 一条：{subagent_id, status, report, rounds_used, error}。
        未知 id → error 条目（服务重启后注册表丢失 → 主 agent 重派）。
        """
        out: list[dict] = []
        running: list[tuple[str, asyncio.Task]] = []
        for sid in ids:
            t = self.get(sid)
            if t is None:
                out.append({"subagent_id": sid, "status": "failed", "report": "",
                            "error": "子代理上下文不存在（服务重启或已过期），请重新派发"})
            elif t.finished_at:
                out.append(_task_result(t))
            else:
                running.append((sid, t.task))
        if running:
            tasks = {task: sid for sid, task in running if task is not None}
            done, pending = await asyncio.wait(tasks.keys(), timeout=timeout)
            for task in done:
                out.append(_task_result(self._tasks[tasks[task]]))
            for task in pending:  # 超时仍在跑：报告 running，任务继续后台执行
                sid = tasks[task]
                t = self._tasks[sid]
                out.append({"subagent_id": sid, "status": "running", "report": "",
                            "rounds_used": t.rounds_used, "error": "仍在执行（wait_all 超时，可稍后 poll）"})
        return out


def _task_result(t: SubagentTask) -> dict:
    return {
        "subagent_id": t.id,
        "status": t.status,
        "report": t.report or "",
        "rounds_used": t.rounds_used,
        "error": t.error,
    }


# 进程级单例
_registry: SubagentRegistry | None = None


def get_subagent_registry() -> SubagentRegistry:
    global _registry
    if _registry is None:
        _registry = SubagentRegistry()
    return _registry
