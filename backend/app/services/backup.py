"""数据库备份：pg_dump 全量备份 + 7 天保留清理。

备份文件命名 {dept_id}_{YYYYMMDD_HHMM}.dump，保留最近 7 天（按文件日期删除）。
"""
from __future__ import annotations

import asyncio
import glob
import os
import signal
from datetime import datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("services.backup")
_settings = get_settings()

BACKUP_DAYS = 7


async def dump_database(dept_id: str, db_name: str | None = None) -> Path | None:
    """pg_dump 数据库 → 备份文件。失败返回 None（不中断同步主流程）。

    db_name 显式指定时用之（如全局库 ai_platform_tardis，三期 M18 数据导入前备份）。
    """
    if db_name is None:
        # 库名统一加前缀（config.db_name_prefix），避免同机多实例撞库
        db_name = (
            f"{_settings.db_name_prefix}tardis_ceo_db"
            if dept_id == "ceo"
            else f"{_settings.db_name_prefix}tardis_dept_{dept_id}_db"
        )
    backup_dir = Path(_settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = backup_dir / f"{dept_id}_{stamp}.dump"

    # L15：凭据读配置（原读相对 CWD 的 .env + 硬编码回退——从别的目录启动静默回退错凭据）
    # M4：host/port 同样从配置 URL 解析（不再硬编码 localhost）；
    # 2026-09-04：删除凭据字面量兜底（解析失败直接抛——配置错误应显性化，不静默用错误凭据）
    import re

    m = re.search(r"://([^:/@]+):([^@]+)@([^/:]+):(\d+)/", _settings.global_db_url)
    if not m:
        raise RuntimeError(f"global_db_url 无法解析（PG 备份需要 db 凭证）: {_settings.global_db_url[:40]}...")
    user, password, host, port = m.group(1), m.group(2), m.group(3), m.group(4)

    env = {**os.environ, "PGPASSWORD": password}
    cmd = ["pg_dump", "-U", user, "-h", host, "-p", port, "-Fc", "-f", str(out_path), db_name]
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # L22：独立进程组，取消时可整组击杀（S11 同类）
    )
    try:
        _, stderr = await proc.communicate()
    finally:
        if proc.returncode is None:  # 备份任务被取消时兜底杀进程组
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    if proc.returncode != 0:
        # 2026-08-21（编码隐患 M2）：stderr 按 UTF-8 严格解码在 GBK 环境会 UnicodeDecodeError
        # 使备份从「记录错误」变崩溃——errors=replace 兜底
        logger.error("pg_dump 失败 dept=%s err=%s", dept_id, stderr.decode("utf-8", errors="replace")[:300])
        return None
    logger.info("备份完成 dept=%s file=%s size=%d", dept_id, out_path.name, out_path.stat().st_size)
    return out_path


async def cleanup_old_backups() -> int:
    """删除超过 7 天的备份文件，返回删除数量。"""
    backup_dir = Path(_settings.backup_dir)
    removed = 0
    for f in glob.glob(str(backup_dir / "*.dump")):
        mtime = os.path.getmtime(f)
        age_days = (datetime.now().timestamp() - mtime) / 86400
        if age_days > BACKUP_DAYS:
            os.remove(f)
            removed += 1
    if removed:
        logger.info("清理过期备份 %d 个", removed)
    return removed
