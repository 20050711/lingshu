"""Celery 实例（三期 M19 渐进式多 worker）。

承载非对话后台任务（定时 job + 同步导入 + 知识摘要 + 视频/简历 batch）。
**Agent 对话 SSE 保持单 uvicorn 进程**（SSE 事件流与交互等待器为内存态，
禁 uvicorn --workers>1；多 worker 需 Redis pub/sub 桥，属四期）。

启动：
    celery -A app.celery_app worker -l info          # worker
    celery -A app.celery_app beat -l info            # 定时调度（仅需一个）
    # 或单进程：celery -A app.celery_app worker -B -l info

渐进式开关：config.celery_enabled（默认 False=仍用 APScheduler 进程内调度；
置 True 后 API 进程不再启动 APScheduler，由 beat 调度）。
"""
from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

_settings = get_settings()

celery_app = Celery(
    "aip",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
    include=["app.tasks"],
)

celery_app.conf.update(
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_acks_late=True,          # 任务执行完才确认（崩溃可重投）
    worker_prefetch_multiplier=1,  # 每 worker 一次取一个任务（避免长任务堆积）
    task_time_limit=3600,         # 单任务硬上限（视频 batch 长任务兜底）
    # 2026-09-17（数据查询线下线）：daily_backup / sync_retry / ceo_sync 三条 beat 与对应任务一同删除
    beat_schedule={
        "session_cleanup": {"task": "app.tasks.task_session_cleanup", "schedule": 12600.0},  # 03:30
        "tools_cleanup": {"task": "app.tasks.task_tools_cleanup", "schedule": 13500.0},      # 03:45
        "memory_scan": {"task": "app.tasks.task_memory_scan", "schedule": 3600.0},           # 每小时
        "memory_extract": {"task": "app.tasks.task_memory_extract", "schedule": 3600.0},     # 每小时（四期记忆提取）
    },
)
