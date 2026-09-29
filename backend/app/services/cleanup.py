"""定期清理：会话/上传文件/产出物 7 天物理删除（PRD：会话 7×24 小时自动清理）。"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, text

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.logging import get_logger
from app.models import ChatFile, ChatMessage, Session

logger = get_logger("services.cleanup")
_settings = get_settings()


async def cleanup_expired_sessions() -> dict:
    """删除超过 session_ttl_days 未活动的会话及其消息/上传文件/产出目录（物理删除）。"""
    engine = get_global_engine()
    cutoff = datetime.now() - timedelta(days=_settings.session_ttl_days)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id FROM sessions WHERE last_activity_at < :c"),
                {"c": cutoff},
            )
        ).all()
    ids = [r[0] for r in rows]
    if not ids:
        return {"deleted_sessions": 0}
    # 取上传文件路径（物理删除用）
    file_paths = []
    async with engine.connect() as conn:
        rows_f = (
            await conn.execute(
                text("SELECT file_path FROM chat_files WHERE session_id = ANY(:ids)"),
                {"ids": ids},
            )
        ).all()
        file_paths = [r[0] for r in rows_f]
    async with engine.begin() as conn:
        await conn.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(ids)))
        await conn.execute(delete(ChatFile).where(ChatFile.session_id.in_(ids)))
        await conn.execute(delete(Session).where(Session.id.in_(ids)))
    # 物理删除：上传文件 + 产出目录（M10：删除后校验残留）
    removed = 0
    for fp in file_paths:
        f = Path(fp)
        if f.exists():
            f.unlink(missing_ok=True)
            removed += 1
    for sid in ids:
        sid = str(sid)  # asyncpg 可能返回 UUID 对象
        d = Path(f"{_settings.output_dir}/{sid}")
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        # 上传目录按 users/{user_id}/{session}/ 组织（四期用户级隔离），按 session 目录清理
        for sub in Path(_settings.upload_dir).glob(f"users/*/{sid}"):
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)

    # 校验：DB 记录清零 + 目录不存在
    async with engine.connect() as conn:
        residual_files = (
            await conn.execute(
                text("SELECT COUNT(*) FROM chat_files WHERE session_id = ANY(:ids)"), {"ids": ids}
            )
        ).scalar()
        residual_sessions = (
            await conn.execute(
                text("SELECT COUNT(*) FROM sessions WHERE id = ANY(:ids)"), {"ids": ids}
            )
        ).scalar()
    residual_dirs = sum(1 for sid in ids if Path(f"{_settings.output_dir}/{sid}").exists())
    logger.info("清理过期会话 %d 个（删除 %d 个上传文件）残留: files=%s sessions=%s dirs=%s",
                len(ids), removed, residual_files, residual_sessions, residual_dirs)
    return {"deleted_sessions": len(ids), "verified_files_removed": removed,
            "residual_files": residual_files, "residual_sessions": residual_sessions,
            "residual_dirs": residual_dirs}


async def cleanup_misc_expired() -> dict:
    """D17（2026-08-10）：清理覆盖面扩展——会话域之外长期无人回收的目录。

    - feedback 截图目录 {upload_dir}/feedback/{uuid}/（mtime > session_ttl_days）
    """
    removed_dirs = 0
    cutoff = datetime.now() - timedelta(days=_settings.session_ttl_days)
    # feedback 截图
    fb_root = Path(_settings.upload_dir) / "feedback"
    if fb_root.exists():
        for d in fb_root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff.timestamp():
                shutil.rmtree(d, ignore_errors=True)
                removed_dirs += 1
    if removed_dirs:
        logger.info("杂项清理完成: feedback 目录 %d 个", removed_dirs)
    return {"feedback_dirs": removed_dirs}
