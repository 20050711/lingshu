"""临时媒体下载路由（2026-08-14，D1 原生视频）：GLM 视觉 video_url 直拉本地上传的视频。

GET /api/v1/tmp-media/{token} → 文件流（无 JWT——GLM 服务器拉取；token 即临时凭证，
services/tmp_media.resolve_tmp_media 校验白名单与 TTL）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.tmp_media import resolve_tmp_media

router = APIRouter(prefix="/tmp-media", tags=["tmp-media"])

VIDEO_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime"}


@router.get("/{token}")
async def get_tmp_media(token: str):
    path = await resolve_tmp_media(token)
    if path is None:
        raise HTTPException(status_code=404, detail="链接不存在或已过期")
    media_type = VIDEO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type, filename=path.name)
