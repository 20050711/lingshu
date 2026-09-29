"""Celery 任务（三期 M19 渐进式：非对话后台任务 worker 化）。

踩坑 27：worker 内勿嵌套 get_event_loop——所有 coroutine 统一用 asyncio.run 包裹；
且 SQLAlchemy async engine 绑定创建时的 loop，asyncio.run 每次新建 loop，
故任务开头先 dispose_engines() 让服务层按当前 loop 重建（worker 进程内任务串行，安全）。
"""
from __future__ import annotations

import asyncio

from app.celery_app import celery_app


def _run(coro_factory):
    """统一执行模式：dispose 引擎 → asyncio.run(coroutine)。"""
    async def _inner():
        from app.core.database import dispose_engines

        await dispose_engines()
        await coro_factory()

    return asyncio.run(_inner())


# 2026-09-17：task_daily_backup / task_sync_retry / task_ceo_sync / task_run_import_job
# 随数据查询线下线删除（团队库/个人库不再有新数据，导入管线不再存在）


@celery_app.task
def task_session_cleanup() -> dict:
    async def _job():
        from app.services.cleanup import cleanup_expired_sessions

        return await cleanup_expired_sessions()

    return _run(_job)


@celery_app.task
def task_tools_cleanup() -> dict:
    async def _job():
        from app.services.tools_cleanup import cleanup_expired_tool_data, cleanup_sandbox_dirs

        result = await cleanup_expired_tool_data()
        removed = await asyncio.to_thread(cleanup_sandbox_dirs)
        return {"tools_cleanup": result, "sandbox_removed": removed}

    return _run(_job)


@celery_app.task
def task_memory_scan() -> dict:
    async def _job():
        from app.services.memory_service import scan_candidates

        return await scan_candidates()

    return _run(_job)


@celery_app.task
def task_memory_extract() -> dict:
    """记忆自动提取（四期）：会话空闲 2 小时后提炼个人记忆。"""
    async def _job():
        from app.services.memory_extract_service import scan_idle_sessions

        return await scan_idle_sessions()

    return _run(_job)


@celery_app.task
def task_kb_summary(doc_id: int, content: str) -> dict:
    """知识文档摘要（kb_service 上传后异步生成）。"""
    async def _job():
        from app.services.kb_service import _kb_summary_job

        await _kb_summary_job(doc_id, content)
        return {"ok": True}

    return _run(_job)


@celery_app.task
def task_kb_summary_retry() -> dict:
    """知识库摘要兜底重试（每日 02:30，KB-REDESIGN L9）。"""
    async def _job():
        from app.services.kb_service import retry_kb_summaries

        return {"retried": await retry_kb_summaries()}

    return _run(_job)
