"""工具下载（2026-09-04）：离线工具包 zip 下载（运维上传 + 简介说明）。

员工端：
- GET /tools/downloads → 启用中的工具包列表（title/desc/filename/size/mtime）
- GET /tools/downloads/{filename} → FileResponse attachment（仅 DB 行白名单，防穿越）

运维端（/admin/tool-downloads，require_admin）：
- GET 全量列表 / POST 上传（multipart: title/desc/file，.zip 魔数校验+分块限流）
- PUT /{id} 改 title/desc/enabled/sort（可选换文件）/ DELETE /{id} 删行+删文件

文件本体在 {tool_downloads_dir}/{stored_path}（uuid8_原始名——防重名覆盖/原始名注入）。
"""
from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.services.config_service import upload_limit_mb
from app.core.file_utils import check_magic_bytes, sanitize_filename, read_limited
from app.core.database import get_global_engine
from app.core.logging import get_logger
from app.core.middleware import get_current_user, require_admin
from app.services import upload_chunks as uc

logger = get_logger("api.tools_download")
router = APIRouter(prefix="/tools/downloads", tags=["tools-downloads"])
admin_router = APIRouter(prefix="/admin/tool-downloads", tags=["tools-downloads-admin"])
_settings = get_settings()


def _row_to_dict(r) -> dict:
    d = dict(r._mapping)
    d["id"] = str(d["id"])
    d["file_url"] = f"/tools/downloads/{d['filename']}"  # 展示用相对 URL（获取需登录）
    upd = d.get("updated_at")
    d["mtime"] = int(upd.timestamp()) if upd else 0
    return d


async def _list(enabled_only: bool) -> list[dict]:
    engine = get_global_engine()
    q = ("SELECT id, title, description, filename, stored_path, size, enabled, sort_order, created_at, updated_at "
         "FROM tool_downloads" + (" WHERE enabled=TRUE" if enabled_only else "") +
         " ORDER BY sort_order, created_at")
    async with engine.connect() as conn:
        rows = (await conn.execute(text(q))).all()
    return [_row_to_dict(r) for r in rows]


# ---------- 员工端 ----------

@router.get("")
async def list_downloads(user: dict = Depends(get_current_user)):
    return {"files": await _list(enabled_only=True)}


@router.get("/{filename}")
async def download_file(filename: str, user: dict = Depends(get_current_user)):
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT stored_path FROM tool_downloads WHERE filename=:f AND enabled=TRUE"),
            {"f": filename})).first()
    if row is None:
        raise HTTPException(404, "文件不存在或已移除")
    path = Path(_settings.tool_downloads_dir) / str(row[0])
    if not path.is_file():
        raise HTTPException(404, "文件躯体不存在（存储目录缺失）？请运维重新上传")
    return FileResponse(path, filename=filename, media_type="application/zip")


# ---------- 运维端 ----------

@admin_router.get("")
async def admin_list(admin: dict = Depends(require_admin)):
    return {"files": await _list(enabled_only=False)}


async def _store_zip(file: UploadFile) -> str:
    """校验 + 流式落盘（返回 stored_path）。校验失败抛 ValueError。

    2026-09-08 修复：原为同步函数（在线程池跑）内直接调 async read_limited →
    拿到 coroutine → check_magic_bytes 崩溃（部署机上/更新工具包 500，
    "'coroutine' object has no attribute 'startswith'"）。改为 async + 落盘走 to_thread。
    """
    name = file.filename or ""
    ext = Path(name).suffix.lower()
    if ext != ".zip":
        raise ValueError("仅支持 zip 工具包")
    d = Path(_settings.tool_downloads_dir)
    d.mkdir(parents=True, exist_ok=True)
    content = await read_limited(file, await upload_limit_mb("tool_pkg_mb") * 1024 * 1024)
    magic_err = check_magic_bytes(content, ext)
    if magic_err:
        raise ValueError(magic_err)
    if not content:
        raise ValueError("文件为空")
    base = sanitize_filename(name) or "tool.zip"
    stored = f"{uuid.uuid4().hex[:10]}_{base}"
    await asyncio.to_thread(Path(d).joinpath(stored).write_bytes, content)
    return stored


async def _store_zip_staged(name: str, src: Path) -> str:
    """分片取件落盘（2026-09-10）：扩展名 + 魔数校验后**移动**（大包不入内存、无单请求大 body）。

    校验失败抛 ValueError（与直传路径同文案）；成功返回 stored_path。
    """
    ext = Path(name or "").suffix.lower()
    if ext != ".zip":
        raise ValueError("仅支持 zip 工具包")
    size = await asyncio.to_thread(lambda: src.stat().st_size)
    if not size:
        raise ValueError("文件为空")
    if size > await upload_limit_mb("tool_pkg_mb") * 1024 * 1024:
        raise ValueError(f"工具包超过 {await upload_limit_mb('tool_pkg_mb')}MB 限制")
    # 魔数只看头部（check_magic_bytes 语义=头字节嗅探，无需全量读入）
    head = await asyncio.to_thread(lambda: src.open("rb").read(8))
    magic_err = check_magic_bytes(head, ext)
    if magic_err:
        raise ValueError(magic_err)
    d = Path(_settings.tool_downloads_dir)
    d.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename(name) or "tool.zip"
    stored = f"{uuid.uuid4().hex[:10]}_{base}"
    await asyncio.to_thread(shutil.move, str(src), str(d / stored))
    return stored


async def _store_from_request(admin: dict, file: UploadFile | None, staged_file: str) -> tuple[str, str]:
    """统一取源落盘 → (展示文件名, stored_path)。

    直传=file；分片=staged_file（2026-09-10：前端 >50MB 先走 /uploads/chunk + /uploads/complete，
    此处取件后移动落盘——不入内存、无单请求大 body）。校验失败抛 HTTPException(400)；
    分片会话取件成功后清理，失败保留供重试（TTL 兜底）。
    """
    staged = uc.parse_staged(staged_file, limit=1)
    if staged:
        name, src = uc.take_staged(admin, staged[0])
        try:
            stored = await _store_zip_staged(name, src)
        except ValueError as e:
            raise HTTPException(400, f"上传校验失败：{e}")
        await asyncio.to_thread(uc.drop_session, staged[0]["upload_id"])
        return name, stored
    if file is None or not file.filename:
        raise HTTPException(400, "请上传工具包文件")
    try:
        stored = await _store_zip(file)
    except ValueError as e:
        raise HTTPException(400, f"上传校验失败：{e}")
    return file.filename, stored


@admin_router.post("")
async def admin_upload(title: str = Form(...), desc: str = Form(""),
                       file: UploadFile | None = File(default=None),
                       staged_file: str = Form(default=""),
                       admin: dict = Depends(require_admin)):
    title = title.strip()[:100]
    if not title:
        raise HTTPException(400, "名称必填")
    filename, stored = await _store_from_request(admin, file, staged_file)
    size = (Path(_settings.tool_downloads_dir) / stored).stat().st_size
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO tool_downloads (title, description, filename, stored_path, size) "
            "VALUES (:t, :d, :f, :s, :z)"),
            {"t": title, "d": desc.strip()[:2000], "f": filename, "s": stored, "z": size})
    logger.info("工具包上传 title=%s file=%s size=%d", title, filename, size)
    return {"ok": True}


@admin_router.put("/{td_id}")
async def admin_update(td_id: str, title: str = Form(...), desc: str = Form(""),
                       enabled: bool = Form(True), sort_order: int = Form(0),
                       file: UploadFile | None = File(None),
                       staged_file: str = Form(default=""),
                       admin: dict = Depends(require_admin)):
    title = title.strip()[:100]
    if not title:
        raise HTTPException(400, "名称必填")
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT stored_path FROM tool_downloads WHERE id::text=:i"),
                                  {"i": td_id})).first()
    if row is None:
        raise HTTPException(404, "记录不存在")
    if (file is not None and file.filename) or staged_file.strip():
        # 换文件：新的落盘，旧文件删（保留 DB 行）；直传/分片取件同路
        new_name, stored_new = await _store_from_request(admin, file, staged_file)
        old = Path(_settings.tool_downloads_dir) / str(row[0])
        old.unlink(missing_ok=True)
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE tool_downloads SET title=:t, description=:d, enabled=:e, sort_order=:o, "
                "filename=:f, stored_path=:s, size=:z, updated_at=NOW() WHERE id::text=:i"),
                {"t": title, "d": desc.strip()[:2000], "e": enabled, "o": sort_order,
                 "f": new_name, "s": stored_new, "z": (Path(_settings.tool_downloads_dir) / stored_new).stat().st_size,
                 "i": td_id})
        return {"ok": True}
    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE tool_downloads SET title=:t, description=:d, enabled=:e, sort_order=:o, updated_at=NOW() "
            "WHERE id::text=:i"),
            {"t": title, "d": desc.strip()[:2000], "e": enabled, "o": sort_order, "i": td_id})
    return {"ok": True}


@admin_router.delete("/{td_id}")
async def admin_delete(td_id: str, admin: dict = Depends(require_admin)):
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT stored_path FROM tool_downloads WHERE id::text=:i"),
                                  {"i": td_id})).first()
    if row is None:
        raise HTTPException(404, "记录不存在")
    (Path(_settings.tool_downloads_dir) / str(row[0])).unlink(missing_ok=True)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM tool_downloads WHERE id::text=:i"), {"i": td_id})
    return {"ok": True}
