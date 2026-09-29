"""工具数据清理（2026-09-28 从批次工具抽取为独立服务）。

- 简历批次：超 tools_data_ttl_days 整体删除（DB + 目录）
- 智能助手媒体缓存（av_cache：转写稿 JSON + 段级视觉文本）：超 av_cache_ttl_days 回收
- 沙箱工作目录：超 sandbox_ttl_days 删除（只删顶层 round 目录，子目录随顶层整体删除）

调度双入口共用：APScheduler（core/scheduler）与 Celery（app/tasks）。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.logging import get_logger

logger = get_logger("services.tools_cleanup")
_settings = get_settings()


async def cleanup_expired_tool_data() -> dict:
    """简历批次 + 媒体缓存过期回收（每日 03:45）。"""
    deleted_db = deleted_dirs = 0
    pending_dirs: list[Path] = []
    engine = get_global_engine()
    async with engine.begin() as conn:
        # 先收集过期 batch_id（精确删除对应目录，不依赖目录 mtime）
        rows = (
            await conn.execute(
                text("SELECT id FROM resume_batches WHERE created_at < NOW() - make_interval(days => :days)"),
                {"days": _settings.tools_data_ttl_days},
            )
        ).all()
        result = await conn.execute(
            text("DELETE FROM resume_batches WHERE created_at < NOW() - make_interval(days => :days)"),
            {"days": _settings.tools_data_ttl_days},
        )
        deleted_db = result.rowcount
        for r in rows:
            pending_dirs.append(Path(f"{_settings.tools_data_dir}/resume/{r.id}"))
    # DB 事务提交后再统一删目录（事务内 rmtree 会长时间占用连接池；崩溃时 DB 回滚但盘已删）
    for d in pending_dirs:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            deleted_dirs += 1

    # 智能助手媒体工具缓存（av_cache：转写稿 JSON + 段级视觉文本）——超 TTL 回收
    av_files = 0
    av_cache_dir = Path(f"{_settings.tools_data_dir}/av_cache")
    if av_cache_dir.exists():
        cutoff = time.time() - _settings.av_cache_ttl_days * 86400
        for f in av_cache_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    av_files += 1
            except OSError:
                continue

    logger.info("工具数据清理完成: db_records=%d dirs=%d 媒体缓存 files=%d",
                deleted_db, deleted_dirs, av_files)
    return {"deleted_db_records": deleted_db, "deleted_dirs": deleted_dirs, "av_cache_files": av_files}


async def cleanup_sandbox_dirs() -> int:
    """沙箱工作目录 > sandbox_ttl_days 删除（只删顶层 round 目录）。

    2026-09-02（凭证误删同类排查）：原 rglob("*") 会递归删任意层目录——work/ 下 1 天未新建
    文件的静态子目录（如只读产出）会被单独 rmtree，即使该轮会话仍在进行。改为只删顶层
    round 目录（work 层），子目录随顶层整体删除，不单独误伤。
    """
    sb_root = Path(_settings.sandbox_dir)
    if not sb_root.exists():
        return 0
    cutoff = time.time() - _settings.sandbox_ttl_days * 86400
    n = 0
    for p in sb_root.iterdir():
        if p.is_dir() and p.stat().st_mtime < cutoff:
            shutil.rmtree(p, ignore_errors=True)
            n += 1
    return n
