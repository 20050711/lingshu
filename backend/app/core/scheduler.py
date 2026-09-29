"""APScheduler 定时任务（一期 + 二期）。

- 03:30 会话/上传文件 7 天清理
- 03:45 工具数据清理（视频/简历批次，7 天；M6）
- 每小时 :10 记忆库候选扫描（M9）

2026-09-17（数据查询线下线）：撤销 01:50 团队库/个人库备份、02:10 同步失败重试、03:00 CEO 库同步
——三者的数据侧（dept_*_db / personal_*_db / tardis_ceo_db）已无新写入，dump 是无意义噪音；
存量 dump 原地保留（不再有清理任务删它们）。
"""
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("core.scheduler")
_settings = get_settings()

_scheduler: AsyncIOScheduler | None = None


def _job_guard(name: str):
    """E-11(API)：定时任务失败告警（原仅日志——备份/清理/CEO 同步失败运维不可见）。"""
    def deco(fn):
        async def wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                logger.error("定时任务 %s 失败: %s", name, str(e)[:300])
                try:
                    from app.services.alert import notify

                    await notify(f"定时任务失败-{name}", str(e)[:200])
                except Exception:
                    pass
        return wrapped
    return deco


@_job_guard("session_cleanup")
async def _job_session_cleanup() -> None:
    from app.services.cleanup import cleanup_expired_sessions, cleanup_misc_expired

    await cleanup_expired_sessions()
    # D17（2026-08-10）：会话域之外的杂项清理（feedback 截图/sync raw 文件/sync_jobs 残留行）
    await cleanup_misc_expired()




@_job_guard("net_probe")
async def _job_net_probe() -> None:
    """外部连通性探针（2026-09-11 加固；每 5 分钟，只做 TCP+TLS 握手零费用）。

    见 services/net_probe：长跑进程对外连接异常时（踩坑 43）及时告警并给出重启命令。
    """
    from app.services.net_probe import run_probe_job

    await run_probe_job()


@_job_guard("tools_cleanup")
async def _job_tools_cleanup() -> None:
    """工具数据清理：简历批次 + 媒体缓存 + 沙箱工作目录超过 TTL 天数后删除（每日 03:45）。"""
    from app.services.tools_cleanup import cleanup_expired_tool_data, cleanup_sandbox_dirs

    result = await cleanup_expired_tool_data()
    # 2026-08-25（会议纪要工具）：录音数据清理（音频 1 天 / 文本+DB 30 天，随工具 TTL）
    try:
        from app.services.meeting_service import cleanup_meeting_data

        await cleanup_meeting_data()
    except Exception as e:
        logger.warning("会议录音数据清理异常: %s", str(e)[:100])
    # 2026-09-09：大文件分片上传临时目录（TTL 24h，未完成/未取件的上传会话丢弃）
    from app.services.upload_chunks import cleanup_expired as cleanup_expired_chunks

    n_chunks = await asyncio.to_thread(cleanup_expired_chunks)
    if n_chunks:
        logger.info("分片上传临时目录已清理 %d 个", n_chunks)
    # 三期 M17：沙箱工作目录 > sandbox_ttl_days 删除（to_thread 防阻塞事件循环）
    removed = await asyncio.to_thread(cleanup_sandbox_dirs)
    logger.info("工具数据清理完成: %s（沙箱目录清理 %d 个）", result, removed)


@_job_guard("memory_scan")
async def _job_memory_scan() -> None:
    """记忆库候选共识扫描（每小时，M9）：激活/替换/过期清理。"""
    from app.services.memory_service import scan_candidates

    await scan_candidates()


@_job_guard("memory_extract")
async def _job_memory_extract() -> None:
    """记忆自动提取（四期）：会话空闲 2 小时后提炼个人记忆（同一小时槽）。"""
    from app.services.memory_extract_service import scan_idle_sessions

    await scan_idle_sessions()


@_job_guard("context_compact")
async def _job_context_compact() -> None:
    """空闲会话被动压缩（2026-09-15）：空闲 ≥ context_compact_idle_hours 且水位达标 →
    结构化压缩（每次最多 3 个，限定 LLM 成本）。"""
    from app.services.context_service import compact_idle_sessions

    n = await compact_idle_sessions(limit=3)
    if n:
        logger.info("空闲会话压缩完成：%d 个", n)


@_job_guard("kb_summary_retry")
async def _job_kb_summary_retry() -> None:
    """知识库摘要兜底重试（02:30，KB-REDESIGN L9：摘要失败不影响检索，后台补齐）。"""
    from app.services.kb_service import retry_kb_summaries

    done = await retry_kb_summaries()
    if done:
        logger.info("知识库摘要兜底重试完成: %d 篇", done)


def init_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    # 2026-08-26：全部 job 加 misfire_grace_time=300——部署机实测周期性延迟 1.5-3.5s
    # （WSL 时钟/事件循环抖动）超过 APScheduler 默认 grace(1s) 导致定时任务被跳过
    # （08-26 备份 01:50 / 多维表格 06:25 均被 miss 丢弃实锤）；5 分钟容忍防再次发生
    _scheduler.add_job(_job_session_cleanup, "cron", hour=3, minute=30, id="session_cleanup",
                       replace_existing=True, misfire_grace_time=300)
    # 2026-09-11：外部连通性探针（每 5 分钟；grace 必须给足——踩坑 43 同类：漏 grace 会被静默跳过）
    _scheduler.add_job(_job_net_probe, "cron", minute="*/5", id="net_probe",
                       replace_existing=True, max_instances=1, misfire_grace_time=300)
    _scheduler.add_job(_job_tools_cleanup, "cron", hour=3, minute=45, id="tools_cleanup",
                       replace_existing=True, misfire_grace_time=300)
    _scheduler.add_job(_job_memory_scan, "cron", minute=_settings.memory_scan_minute, id="memory_scan",
                       replace_existing=True, misfire_grace_time=300)
    _scheduler.add_job(_job_memory_extract, "cron", minute=_settings.memory_scan_minute, id="memory_extract",
                       replace_existing=True, misfire_grace_time=300)
    # 2026-09-15：空闲会话被动压缩（每小时错开 10 分钟；无候选时零开销）
    _scheduler.add_job(_job_context_compact, "cron", minute=(_settings.memory_scan_minute + 10) % 60,
                       id="context_compact", replace_existing=True, max_instances=1, misfire_grace_time=300)
    _scheduler.add_job(_job_kb_summary_retry, "cron", hour=2, minute=30, id="kb_summary_retry",
                       replace_existing=True, misfire_grace_time=300)
    _scheduler.start()
    logger.info("APScheduler 已启动（工具清理 03:45 / 会话清理 03:30 / "
                "记忆扫描每小时:%s / 空闲压缩每小时:%s / 连通性探针 */5）",
                _settings.memory_scan_minute, (_settings.memory_scan_minute + 10) % 60)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def list_jobs() -> list[dict]:
    """E-11(API)：实时定时任务清单（/admin/jobs 端点用——原硬编码只有 2 个 job 与实际 8 个不符）。"""
    if _scheduler is None:
        return []
    jobs = []
    for j in _scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "next_run": str(j.next_run_time) if j.next_run_time else None,
        })
    return jobs
