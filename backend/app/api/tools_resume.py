"""工具集-简历初筛 API（二期）。

- POST /tools/resume/batches         上传简历（≤20 份，sha1 去重）→ 后台解析+评分
- GET  /tools/resume/batches/{id}    轮询状态 + 结果列表（排名/维度分/总分/评语）
- POST /tools/resume/batches/{id}/score  提交 JD + 7 维度权重，触发评分
- GET  /tools/resume/batches/{id}/export 导出 ZIP（原件+评分详情+排名汇总）
- POST /tools/resume/parse           解耦：单文件 → 文本（邮箱自动化预留）
- POST /tools/resume/score-one       解耦：{content,jd,weights} → 评分（邮箱自动化预留）
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy import text

from app.core.config import get_settings
from app.services.config_service import upload_limit_mb
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.file_utils import read_limited, sanitize_filename
from app.core.logging import get_logger
from app.core.middleware import get_current_user
from app.api.tools_guard import require_dept_custom_tool
from app.services.resume_service import (
    DIMENSIONS,
    build_export_zip,
    launch_batch,
    parse_resume,
    score_one,
    sha1_file,
)

logger = get_logger("api.tools_resume")
_settings = get_settings()
router = APIRouter(tags=["tools-resume"])

RESUME_EXTS = {".pdf", ".docx", ".jpg", ".jpeg", ".png"}


def _check_weights(weights: dict) -> dict:
    if not isinstance(weights, dict):
        raise app_error("E011", "权重格式错误", status_code=400)
    unknown = set(weights) - set(DIMENSIONS)
    if unknown:
        raise app_error("E011", f"未知维度: {unknown}", status_code=400)
    return weights


@router.post("/tools/resume/batches")
async def create_resume_batch(
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_dept_custom_tool("resume")),
):
    """上传简历批次（去重）。返回 batch_id 与重复文件列表。"""
    if len(files) > _settings.resume_max_count:
        raise app_error("E003", f"单批最多 {_settings.resume_max_count} 份简历", status_code=400)

    engine = get_global_engine()
    batch_dir = Path(f"{_settings.tools_data_dir}/resume")
    batch_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    batch_id = None
    duplicates: list[str] = []
    contents: list[tuple[str, bytes]] = []

    for f in files:
        fname = sanitize_filename(f.filename, "resume")
        ext = Path(fname).suffix.lower()
        if ext not in RESUME_EXTS:
            raise app_error("E003", f"不支持的简历格式 {ext}（支持 pdf/docx/jpg/png）", status_code=400)
        try:  # M3：分块读取限流（原无单文件上限）
            content = await read_limited(f, await upload_limit_mb("kb_mb") * 1024 * 1024)
        except ValueError as e:
            raise app_error("E003", f"{e}: {fname}", status_code=400)
        if not content:
            raise app_error("E003", f"文件为空: {fname}", status_code=400)
        contents.append((fname, content))

    async with engine.begin() as conn:
        batch_id = (
            await conn.execute(
                text("INSERT INTO resume_batches (user_id, status) VALUES (:u, 'pending') RETURNING id"),
                {"u": user["user_id"]},
            )
        ).scalar()
        item_dir = Path(f"{_settings.tools_data_dir}/resume/{batch_id}")
        item_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in contents:
            file_path = item_dir / f"{stamp}_{fname}"
            await asyncio.to_thread(file_path.write_bytes, content)  # M9：同步写盘包线程
            sha1 = await asyncio.to_thread(sha1_file, str(file_path))  # M9：同步读盘包线程
            dup = (
                await conn.execute(
                    text("SELECT 1 FROM resume_items WHERE sha1=:s AND batch_id=:b LIMIT 1"),
                    {"s": sha1, "b": batch_id},
                )
            ).first()
            if dup:
                file_path.unlink(missing_ok=True)
                duplicates.append(fname)
                continue
            await conn.execute(
                text("INSERT INTO resume_items (batch_id, file_name, file_path, sha1, status) "
                     "VALUES (:b, :n, :p, :s, 'uploaded')"),
                {"b": batch_id, "n": fname, "p": str(file_path), "s": sha1},
            )
    return {"batch_id": batch_id, "duplicates": duplicates}


async def _get_batch(batch_id: str, user: dict) -> tuple[dict, list]:
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, status, jd, weights, top_k, created_at FROM resume_batches WHERE id=:id"),
                {"id": batch_id},
            )
        ).first()
        if row is None:
            raise app_error("E011", "批次不存在", status_code=404)
        owner = (
            await conn.execute(text("SELECT user_id FROM resume_batches WHERE id=:id"), {"id": batch_id})
        ).scalar()
        if owner != user["user_id"]:
            raise app_error("E006", "无权访问该批次", status_code=403)
        items = (
            await conn.execute(
                text("SELECT id, file_name, status, dim_scores, total_score, rank, comment, error_msg "
                     "FROM resume_items WHERE batch_id=:b ORDER BY rank NULLS LAST, created_at"),
                {"b": batch_id},
            )
        ).all()
    batch = {
        "batch_id": row.id, "status": row.status, "jd": row.jd, "weights": row.weights,
        "top_k": row.top_k, "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    item_list = [
        {
            "item_id": i.id, "file_name": i.file_name, "status": i.status,
            "dim_scores": i.dim_scores, "total_score": i.total_score, "rank": i.rank,
            "comment": i.comment, "error": i.error_msg,
        }
        for i in items
    ]
    return batch, item_list


@router.delete("/tools/resume/batches/{batch_id}")
async def delete_resume_batch(batch_id: str, user: dict = Depends(require_dept_custom_tool("resume"))):
    """删除简历批次（DB 记录；简历文件按 SHA1 跨批次共享，交由 TTL 清理，不在此删除）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        owner = (
            await conn.execute(text("SELECT user_id FROM resume_batches WHERE id=:id"), {"id": batch_id})
        ).scalar()
        if owner is None:
            raise app_error("E011", "批次不存在", status_code=404)
        if owner != user["user_id"]:
            raise app_error("E006", "无权访问该批次", status_code=403)
        await conn.execute(text("DELETE FROM resume_items WHERE batch_id=:b"), {"b": batch_id})
        await conn.execute(text("DELETE FROM resume_batches WHERE id=:id"), {"id": batch_id})
    return {"ok": True}


@router.get("/tools/resume/batches")
async def list_resume_batches(user: dict = Depends(require_dept_custom_tool("resume"))):
    """我的简历批次历史列表（最近 20 条，含完成数）——切页后恢复任务记录的入口。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT rb.id, rb.status, rb.created_at, "
                     "(SELECT COUNT(*) FROM resume_items ri WHERE ri.batch_id = rb.id) AS item_count, "
                     "(SELECT COUNT(*) FROM resume_items ri WHERE ri.batch_id = rb.id AND ri.status IN ('done','scored')) AS done_count "
                     "FROM resume_batches rb WHERE rb.user_id = :u ORDER BY rb.created_at DESC LIMIT 20"),
                {"u": user["user_id"]},
            )
        ).all()
    return {"batches": [
        {"batch_id": r.id, "status": r.status, "created_at": str(r.created_at),
         "item_count": r.item_count, "done_count": r.done_count}
        for r in rows
    ]}


@router.get("/tools/resume/batches/{batch_id}")
async def get_resume_batch(batch_id: str, user: dict = Depends(require_dept_custom_tool("resume"))):
    batch, items = await _get_batch(batch_id, user)
    return {"batch": batch, "items": items}


@router.post("/tools/resume/batches/{batch_id}/score")
async def score_resume_batch(batch_id: str, body: dict, user: dict = Depends(require_dept_custom_tool("resume"))):
    """提交 JD 与权重，触发后台评分。body: {"jd": str, "weights": {...}, "aux_model": {...}|null}"""
    jd = str(body.get("jd") or "").strip()
    weights = _check_weights(body.get("weights") or {d: 1 for d in DIMENSIONS})
    top_k = min(max(int(body.get("top_k") or _settings.resume_top_k_default), 1), 20)
    # D3：批次级辅助模型覆盖（{platform,model}；空=走 llm_aux 分层）
    aux = body.get("aux_model")
    if not (isinstance(aux, dict) and aux.get("model")):
        aux = None

    await _get_batch(batch_id, user)  # 归属校验
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE resume_batches SET jd=:j, weights=:w, top_k=:k, status='pending', "
                 "aux_model=CAST(:aux AS JSONB), updated_at=NOW() WHERE id=:id"),
            {"j": jd, "w": json.dumps(weights, ensure_ascii=False), "k": top_k, "id": batch_id,
             "aux": json.dumps(aux, ensure_ascii=False) if aux else None},
        )
    launch_batch(batch_id)
    return {"ok": True, "status": "queued"}


@router.get("/tools/resume/batches/{batch_id}/export")
async def export_resume_batch(batch_id: str, user: dict = Depends(require_dept_custom_tool("resume"))):
    batch, items = await _get_batch(batch_id, user)
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT file_name, file_path, text_content AS text, dim_scores, total_score, rank, comment "
                     "FROM resume_items WHERE batch_id=:b ORDER BY rank NULLS LAST"),
                {"b": batch_id},
            )
        ).all()
    data = [
        {"file_name": r.file_name, "file_path": r.file_path, "text": r.text,
         "dim_scores": r.dim_scores, "total_score": r.total_score, "rank": r.rank, "comment": r.comment}
        for r in rows
    ]
    top_k = batch.get("top_k") or _settings.resume_top_k_default
    content = await asyncio.to_thread(  # M9：zip 压缩与逐文件读盘包线程
        build_export_zip, data, batch.get("jd") or "", batch.get("weights") or {}, top_k
    )
    # 问题 12（2026-08-17）：zip 名用职位描述摘要（可读）替代 uuid 批次前缀
    jd_name = (batch.get("jd") or "简历筛选").replace("/", "_").replace("\\", "_").strip()[:20] or "简历筛选"
    # 2026-08-21（编码隐患 H1）：filename 直放中文 → Starlette latin-1 编码 500（每次导出必现）；
    # 同 2026-08-20 chat.py 修复——RFC 5987 filename* UTF-8 双轨；jd 首 20 字可能含 " 换行等，一并 quote
    import urllib.parse

    fallback = "resume_rank.zip"  # 纯 ASCII 兜底（RFC 5987 双轨的 filename 部分）
    display = f"{jd_name}_筛选结果.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{urllib.parse.quote(display)}'},
    )


# ---------------------------------------------------------------- 解耦接口（邮箱自动化预留）

@router.post("/tools/resume/parse")
async def parse_resume_api(file: UploadFile = File(...), user: dict = Depends(require_dept_custom_tool("resume"))):
    fname = file.filename or "resume"
    ext = Path(fname).suffix.lower()
    if ext not in RESUME_EXTS:
        raise app_error("E003", f"不支持的简历格式 {ext}", status_code=400)
    try:  # M3：分块读取限流
        content = await read_limited(file, await upload_limit_mb("kb_mb") * 1024 * 1024)
    except ValueError as e:
        raise app_error("E003", str(e), status_code=400)
    tmp_dir = Path(_settings.temp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"resume_parse_{user['user_id']}_{time.time():.0f}{ext}"
    await asyncio.to_thread(tmp.write_bytes, content)  # M9：同步写盘包线程
    try:
        text_content = await parse_resume(str(tmp), fname)
    finally:
        tmp.unlink(missing_ok=True)
    return {"text": text_content}


@router.post("/tools/resume/score-one")
async def score_one_api(body: dict, user: dict = Depends(require_dept_custom_tool("resume"))):
    content = str(body.get("content") or "").strip()
    if not content:
        raise app_error("E011", "缺少简历内容", status_code=400)
    weights = _check_weights(body.get("weights") or {d: 1 for d in DIMENSIONS})
    result = await score_one(content, str(body.get("jd") or ""), weights)
    return result
