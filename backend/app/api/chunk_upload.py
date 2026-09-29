"""通用分片上传 API（2026-09-10；协议与消费见 services/upload_chunks.py）。

- POST /uploads/chunk     保存单片（upload_id + chunk_index + chunk，幂等）
- POST /uploads/complete  齐备校验 + 合并 + 位级校验 → 返回暂存文件凭据 {upload_id, file_name}

消费侧：业务接口（会议纪要 /tools/meetings、工具包上传等）以
staged_files（JSON 数组 [{upload_id, file_name}]）接收，取件走 upload_chunks.take_staged。
各业务接口在 complete 后以 staged_files 接收合并产物（取件即清理）。

鉴权：登录即可（分片只写自己的会话目录，.owner 归属校验；取件侧仍各自查业务工具白名单）。
限额：单片 ≤ config upload_chunk_max_mb（前端 50MB 切分）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.core.config import get_settings
from app.core.exceptions import app_error
from app.core.middleware import get_current_user
from app.services import upload_chunks as uc

router = APIRouter(prefix="/uploads", tags=["uploads"])
_settings = get_settings()

_MAX_CHUNKS = 100000  # 与协议上限对齐（单片 50MB → 上限 4.8TB，实际受业务上限约束）


@router.post("/chunk")
async def upload_chunk(
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """保存单个分片（幂等：同 index 重复上传覆盖）。"""
    return await uc.save_chunk(user, upload_id, chunk_index, chunk)


@router.get("/progress/{upload_id}")
async def upload_progress(upload_id: str, user: dict = Depends(get_current_user)):
    """服务端阶段进度（合并/解压）：complete 挂起期间前端 1s 轮询刷进度条。

    只暴露百分比与一句文案（不含文件名等），登录即可读；无记录回空态。
    """
    return await uc.get_progress(upload_id)


@router.delete("/chunk/{upload_id}")
async def upload_cancel(upload_id: str, user: dict = Depends(get_current_user)):
    """显式取消上传：删掉分片会话目录（2026-09-16 走查——用户"取消"后几 GB 分片要等 24h TTL 才清）。

    只允许清理自己的会话（.owner 校验）；幂等，会话不存在也返回 ok。
    """
    d = uc.session_dir(upload_id)          # upload_id 合法性校验（非法直接 400）
    uc.assert_owner(d, user["user_id"])
    await asyncio.to_thread(uc.drop_session, upload_id)
    return {"ok": True}


@router.post("/complete")
async def upload_complete(
    upload_id: str = Form(...),
    total_chunks: int = Form(...),
    total_size: int = Form(...),
    md5: str = Form(""),
    file_name: str = Form(""),
    user: dict = Depends(get_current_user),
):
    """合并并校验 → 返回暂存凭据（不跑业务管线；业务接口再凭 upload_id 取件消费）。"""
    if total_chunks < 1 or total_chunks > _MAX_CHUNKS:
        raise app_error("E003", "total_chunks 非法", status_code=400)
    path = await uc.merge_chunks(user, upload_id, total_chunks, total_size, md5)
    return {"upload_id": upload_id,
            "file_name": (file_name or "upload.bin")[:200],
            "size": path.stat().st_size}
