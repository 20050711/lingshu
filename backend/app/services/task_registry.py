"""会话任务注册表（2026-08-18 任务后台化）：进程级 session_id → 运行中 Agent 任务。

任务与 SSE 请求生命周期解耦：断连只注销转发器（tail_stream），agent/relay 任务继续；
显式停止（POST /chat/stop）经 terminated 信号 + agent_task.cancel() 终止。
单 uvicorn 进程约束（celery_app.py 声明禁 --workers>1）下，进程级注册表成立。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# 2026-08-19（架构 P0）：EventBroadcaster 下沉 app/services/events.py（零依赖本模块），
# TYPE_CHECKING 改真实 import——打破原 chat_service ↔ task_registry 循环依赖
from app.services.events import EventBroadcaster


@dataclass
class TaskRecord:
    """一个运行中（或刚结束）的 Agent 任务。"""

    session_id: str
    round_id: int
    question: str
    broadcaster: EventBroadcaster
    agent_task: asyncio.Task | None = None   # start_bg_task 中 create_task 后赋值
    relay_task: asyncio.Task | None = None
    terminated: asyncio.Event = field(default_factory=asyncio.Event)
    final_state: dict | None = None
    error_message: str | None = None
    status: str = "running"  # running/completed/error/interrupted


_TASKS: dict[str, TaskRecord] = {}


def get_task(session_id: str) -> TaskRecord | None:
    return _TASKS.get(session_id)


def register(rec: TaskRecord) -> None:
    _TASKS[rec.session_id] = rec


def unregister(rec: TaskRecord) -> None:
    if _TASKS.get(rec.session_id) is rec:
        _TASKS.pop(rec.session_id, None)


def running_count() -> int:
    return sum(1 for r in _TASKS.values() if r.status == "running")
