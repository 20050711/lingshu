"""会议纪要工具 API（2026-08-25 定制化工具；需求见 docs/会积纪要工具.md）。

- POST   /tools/meetings            上传录音（multipart file + title），后台转写
- GET    /tools/meetings            我的录音列表（最近 20 条）
- GET    /tools/meetings/{id}       轮询详情（status/transcript/summary/scene/error）
- POST   /tools/meetings/{id}/summarize   生成总结（body {scene, custom_prompt?}；仅 ready 可总结）
- GET    /tools/meetings/{id}/download    zip 下载（原始录音 + 语音转写.md + 总结.md）
- DELETE /tools/meetings/{id}       删除录音（取消任务 + 清目录 + 删 DB）
- GET    /tools/meetings/scenes     场景下拉数据源

状态机：uploaded → transcribing → ready → summarizing → done / failed
入口统一 require_custom_tool("meeting")（团队白名单；CUSTOM_TOOLS 注册表）。
"""
from __future__ import annotations

import asyncio
import io as _io
import shutil
import urllib.parse
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy import text

from app.core.config import get_settings
from app.services.config_service import upload_limit_mb
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.file_utils import read_limited, sanitize_filename, sanitize_zip_member
from app.core.logging import get_logger
from app.core.middleware import get_current_user
from app.api.tools_guard import require_custom_tool
from app.services import upload_chunks as uc
from app.agent.prompts.meeting_tasks import MEETING_SCENES

logger = get_logger("api.tools_meeting")
_settings = get_settings()
router = APIRouter(tags=["tools-meeting"])

# 2026-08-25（用户要求支持 mp3/m4a 等常见音频）：ffmpeg 抽音频兜底，放宽到 ffmpeg 常见输入格式
MEETING_EXTS = {".webm", ".m4a", ".wav", ".ogg", ".mp3", ".mp4", ".aac", ".flac", ".wma", ".amr", ".opus", ".aiff", ".mpeg", ".oga", ".3gp"}


async def _get_meeting(meeting_id: str, user: dict):
    """归属校验 + 返回记录行（404/403）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, user_id, status, title, duration_s, transcript, summary, scene, "
                     "custom_prompt, transcript_path, summary_path, server_path, sys_path, merged_path, error_msg, created_at "
                     "FROM meeting_recordings WHERE id=:id"), {"id": meeting_id},
            )
        ).first()
    if row is None:
        raise app_error("E011", "录音不存在", status_code=404)
    if row.user_id != user["user_id"]:
        raise app_error("E006", "无权访问该录音", status_code=403)
    return row


def _meeting_dir(meeting_id: str) -> Path:
    return Path(f"{_settings.tools_data_dir}/meeting/{meeting_id}")


@router.post("/tools/meetings")
async def create_meeting(
    file: UploadFile | None = File(default=None),
    sys_file: UploadFile | None = File(default=None),  # 2026-08-25 双轨：线上轨（系统声音）；空=单轨
    staged_file: str = Form(default=""),        # 2026-09-10：分片暂存凭据（大录音切片上传后取件）
    staged_sys_file: str = Form(default=""),
    title: str = Form(default=""),
    user: dict = Depends(require_custom_tool("meeting")),
):
    """上传录音文件 → 落盘 + 插行 uploaded → 后台转写。双轨（file=本地麦克风 + sys_file=线上系统声音）。

    2026-09-10：>50MB 录音走 /uploads/chunk 切片 + /uploads/complete 合并，本接口以
    staged_file/staged_sys_file（[{upload_id, file_name}]）取件（单请求大 body 会撞
    本机 nginx 600m / 部署机 portproxy >650MB）。
    """
    limit = await upload_limit_mb("video_mb") * 1024 * 1024
    staged_main = uc.parse_staged(staged_file, limit=1)
    staged_sys = uc.parse_staged(staged_sys_file, limit=1)
    if file is None and not staged_main:
        raise app_error("E003", "请上传录音文件", status_code=400)

    content: bytes | None = None
    src_path: Path | None = None
    if staged_main:
        fname = sanitize_filename(staged_main[0]["file_name"], "recording")
        ext = Path(fname).suffix.lower()
        if ext not in MEETING_EXTS:
            raise app_error("E003", f"不支持的文件格式 {ext}（支持 {sorted(MEETING_EXTS)}）", status_code=400)
        _orig, src_path = uc.take_staged(user, staged_main[0])
        if src_path.stat().st_size > limit:
            raise app_error("E003", f"文件超过 {limit // (1024 * 1024)}MB 限制: {fname}", status_code=400)
        if src_path.stat().st_size == 0:
            raise app_error("E003", f"文件为空: {fname}", status_code=400)
    else:
        fname = sanitize_filename(file.filename, "recording")
        ext = Path(fname).suffix.lower()
        if ext not in MEETING_EXTS:
            raise app_error("E003", f"不支持的文件格式 {ext}（支持 {sorted(MEETING_EXTS)}）", status_code=400)
        try:
            content = await read_limited(file, limit)
        except ValueError as e:
            raise app_error("E003", f"{e}: {fname}", status_code=400)
        if not content:
            raise app_error("E003", f"文件为空: {fname}", status_code=400)

    sys_content: bytes | None = None
    sys_src: Path | None = None
    if staged_sys:
        sname = sanitize_filename(staged_sys[0]["file_name"], "recording")
        sext = Path(sname).suffix.lower()
        if sext not in MEETING_EXTS:
            raise app_error("E003", f"不支持的文件格式 {sext}（支持 {sorted(MEETING_EXTS)}）", status_code=400)
        _o, sys_src = uc.take_staged(user, staged_sys[0])
        if sys_src.stat().st_size > limit:
            raise app_error("E003", f"文件超过 {limit // (1024 * 1024)}MB 限制: {sname}", status_code=400)
    elif sys_file is not None and sys_file.filename:
        sname = sanitize_filename(sys_file.filename, "recording")
        sext = Path(sname).suffix.lower()
        if sext not in MEETING_EXTS:
            raise app_error("E003", f"不支持的文件格式 {sext}（支持 {sorted(MEETING_EXTS)}）", status_code=400)
        try:
            sys_content = await read_limited(sys_file, limit)
        except ValueError as e:
            raise app_error("E003", f"{e}: {sname}", status_code=400)

    from app.services.meeting_service import _launch_meeting

    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("INSERT INTO meeting_recordings (user_id, title, status, dept_id, user_role) "
                     "VALUES (:u, :t, 'uploaded', :d, :r) RETURNING id"),
                {"u": user["user_id"], "t": title.strip() or None,
                 "d": user.get("dept_id"), "r": user.get("role")},
            )
        ).first()
    meeting_id = str(row[0])
    item_dir = _meeting_dir(meeting_id)
    item_dir.mkdir(parents=True, exist_ok=True)
    audio_path = item_dir / f"录音_{meeting_id[:8]}{ext}"
    if src_path is not None:
        await asyncio.to_thread(shutil.move, str(src_path), str(audio_path))   # 分片暂存：移动不过内存
    else:
        audio_path.write_bytes(content)
    sys_path: str | None = None
    if sys_src is not None:
        sys_path = str(item_dir / f"线上_{meeting_id[:8]}{Path(fname).suffix}")
        await asyncio.to_thread(shutil.move, str(sys_src), sys_path)
    elif sys_content:
        sys_path = str(item_dir / f"线上_{meeting_id[:8]}{Path(fname).suffix}")
        Path(sys_path).write_bytes(sys_content)
    # 2026-09-10：取件完成的暂存会话即时清理（未消费的残留由每日 TTL 兜底）
    for st in staged_main + staged_sys:
        await asyncio.to_thread(uc.drop_session, st["upload_id"])
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE meeting_recordings SET server_path=:p, sys_path=:s, updated_at=NOW() WHERE id=:id"),
            {"p": str(audio_path), "s": sys_path, "id": meeting_id},
        )
    _launch_meeting(meeting_id)
    return {"meeting_id": meeting_id, "status": "uploaded"}


@router.get("/tools/meetings")
async def list_meetings(user: dict = Depends(require_custom_tool("meeting"))):
    """我的录音历史列表（最近 20 条）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, status, title, duration_s, scene, created_at, error_msg "
                     "FROM meeting_recordings WHERE user_id=:u ORDER BY created_at DESC LIMIT 20"),
                {"u": user["user_id"]},
            )
        ).all()
    return {"meetings": [
        {"meeting_id": r.id, "status": r.status, "title": r.title, "duration_s": r.duration_s,
         "scene": r.scene, "created_at": str(r.created_at), "error_msg": r.error_msg}
        for r in rows
    ]}


@router.get("/tools/meetings/scenes")
async def list_meeting_scenes(user: dict = Depends(require_custom_tool("meeting"))):
    """总结场景下拉数据源（预设场景 + 自定义）。注意：须注册在 {meeting_id} 路由之前（避免被当作 id 拦截）。"""
    return {"scenes": [{"key": k, "label": v["label"]} for k, v in MEETING_SCENES.items()]}


@router.get("/tools/meetings/{meeting_id}")
async def get_meeting(meeting_id: str, user: dict = Depends(require_custom_tool("meeting"))):
    """轮询详情（前端 3s 间隔）。"""
    row = await _get_meeting(meeting_id, user)
    return {
        "meeting_id": row.id, "status": row.status, "title": row.title, "duration_s": row.duration_s,
        "transcript": row.transcript or "", "summary": row.summary or "",
        "scene": row.scene, "custom_prompt": row.custom_prompt, "error_msg": row.error_msg,
        "created_at": str(row.created_at),
    }


@router.post("/tools/meetings/{meeting_id}/summarize")
async def summarize_meeting(meeting_id: str, body: dict, user: dict = Depends(require_custom_tool("meeting"))):
    """生成总结：scene=预设场景 key（空=仅自定义），custom_prompt 为额外要求（2026-08-27 起与场景并存）。
    ready/done 均可（done=重新分析，覆盖原总结）。
    aux_model 可选：总结模型覆盖（{platform, model, thinking?}，空=llm_aux meeting 档→默认）。"""
    from app.services.meeting_service import summarize_meeting as _summarize

    row = await _get_meeting(meeting_id, user)
    if row.status not in ("ready", "done"):
        raise app_error("E011", f"当前状态 {row.status} 不可生成总结（需转写完成）", status_code=400)
    scene = str(body.get("scene") or "")
    custom_prompt = str(body.get("custom_prompt") or "").strip()
    if scene and scene not in MEETING_SCENES:
        raise app_error("E011", f"未知总结场景: {scene}", status_code=400)
    if not scene and not custom_prompt:
        raise app_error("E011", "请选择总结场景或输入自定义提示词", status_code=400)
    # 2026-08-25（用户要求）：总结模型选择（前端传 aux_model 覆盖 llm_aux 档）
    aux_model: dict | None = body.get("aux_model")
    if aux_model is not None:
        if not isinstance(aux_model, dict) or not aux_model.get("model"):
            raise app_error("E011", "aux_model 须为 {platform, model, thinking?} 且 model 必填", status_code=400)
        aux_model = {
            "platform": str(aux_model.get("platform") or "deepseek"),
            "model": str(aux_model["model"]),
            "effort": str(aux_model.get("effort") or "") if aux_model.get("effort") else "",
            "thinking": aux_model.get("thinking"),
        }
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE meeting_recordings SET status='summarizing', updated_at=NOW() WHERE id=:id"),
            {"id": meeting_id},
        )
    _summarize(meeting_id, scene, custom_prompt, aux_model)
    return {"ok": True, "status": "summarizing"}


@router.post("/tools/meetings/{meeting_id}/retry")
async def retry_meeting_transcribe(meeting_id: str, user: dict = Depends(require_custom_tool("meeting"))):
    """重新转写（2026-08-27 用户要求）：failed 记录复用已落盘音频重跑转写，无需重新上传/录音。
    要求：status=failed 且原始录音（或合成音频）文件仍在。"""
    from app.services.meeting_service import _launch_meeting

    row = await _get_meeting(meeting_id, user)
    if row.status != "failed":
        raise app_error("E011", f"当前状态 {row.status} 不可重新转写（需处理失败）", status_code=400)
    audio = row.merged_path or row.server_path
    if not audio or not Path(audio).exists():
        raise app_error("E011", "录音文件已不存在（已清理），无法重新转写", status_code=400)
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE meeting_recordings SET status='transcribing', error_msg=NULL, "
                 "transcript=NULL, transcript_path=NULL, summary=NULL, summary_path=NULL, "
                 "updated_at=NOW() WHERE id=:id"),
            {"id": meeting_id},
        )
    _launch_meeting(meeting_id)
    return {"ok": True, "status": "transcribing"}


@router.get("/tools/meetings/{meeting_id}/transcript")
async def download_meeting_transcript(meeting_id: str, user: dict = Depends(require_custom_tool("meeting"))):
    """下载转写稿 zip 包（2026-08-25 用户要求：zip 格式）——内含语音转写.md + 完整音频（合成或原始）。
    转写完成即可下载，无需等总结；failed 也可下载（仅音频，2026-08-27：转写不成功至少能拿到录音）；
    RFC 5987 中文名双轨。"""
    from fastapi.responses import Response

    row = await _get_meeting(meeting_id, user)
    if row.status not in ("ready", "done", "failed"):
        raise app_error("E011", "转写稿尚未生成", status_code=400)
    has_transcript = row.transcript_path and Path(row.transcript_path).exists()
    audio = row.merged_path or row.server_path
    has_audio = audio and Path(audio).exists()
    if not has_transcript and not has_audio:
        raise app_error("E011", "转写稿尚未生成（录音文件也不存在）", status_code=400)
    # 2026-09-10 同类修复（走查：导出的 zip 在 Windows 打开是空的）：标题里的 Windows 非法
    # 字符（| : * ? " < >）会让整包在资源管理器里打不开——归档名一律过净化
    safe_title = sanitize_zip_member(row.title or "会议纪要")[:80] or "会议纪要"
    root = f"语音转写_{safe_title}"
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if has_transcript:
            zf.write(row.transcript_path, arcname=f"{root}/语音转写.md")
        # 完整音频：合成（双轨）或原始（单轨）；缺文件容错
        if has_audio:
            zf.write(audio, arcname=sanitize_zip_member(f"{root}/完整音频/{Path(audio).name}"), compress_type=zipfile.ZIP_STORED)
    buf.seek(0)
    zip_name = f"语音转写_{safe_title}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 f"attachment; filename=\"transcript_{meeting_id[:8]}.zip\"; filename*=UTF-8''{urllib.parse.quote(zip_name)}"},
    )


@router.get("/tools/meetings/{meeting_id}/download")
async def download_meeting(meeting_id: str, user: dict = Depends(require_custom_tool("meeting"))):
    """下载完整档案 zip：原始录音（STORED）+ 语音转写.md + 总结.md（DEFLATED）。"""
    row = await _get_meeting(meeting_id, user)
    if row.status != "done" or not row.summary_path or not Path(row.summary_path).exists():
        raise app_error("E011", "总结尚未生成", status_code=400)
    item_dir = Path(row.summary_path).parent
    # 2026-09-10 同类修复（走查：导出的 zip 在 Windows 打开是空的）：标题里的 Windows 非法
    # 字符（| : * ? " < >）会让整包在资源管理器里打不开——归档名一律过净化
    safe_title = sanitize_zip_member(row.title or "会议纪要")[:80] or "会议纪要"
    root = f"会议纪要_{safe_title}"
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 2026-08-25（用户要求）：合成好的完整音频优先（双轨合成 m4a）；单轨直接用原文件
        if row.merged_path and Path(row.merged_path).exists():
            zf.write(row.merged_path, arcname=sanitize_zip_member(f"{root}/完整音频/{Path(row.merged_path).name}"),
                     compress_type=zipfile.ZIP_STORED)
        elif row.server_path and Path(row.server_path).exists():
            zf.write(row.server_path, arcname=sanitize_zip_member(f"{root}/完整音频/{Path(row.server_path).name}"),
                     compress_type=zipfile.ZIP_STORED)  # 音频已压缩，STORED 避免无谓 CPU
        # 原始音轨（双轨时两路独立文件，便于二次处理）
        if row.server_path and Path(row.server_path).exists() and row.merged_path and Path(row.merged_path).exists():
            zf.write(row.server_path, arcname=sanitize_zip_member(f"{root}/原始音轨/本地麦克风/{Path(row.server_path).name}"),
                     compress_type=zipfile.ZIP_STORED)
            if row.sys_path and Path(row.sys_path).exists():
                zf.write(row.sys_path, arcname=sanitize_zip_member(f"{root}/原始音轨/线上系统声音/{Path(row.sys_path).name}"),
                         compress_type=zipfile.ZIP_STORED)
        if row.transcript_path and Path(row.transcript_path).exists():
            zf.write(row.transcript_path, arcname=f"{root}/语音转写/语音转写.md")
        if row.summary_path and Path(row.summary_path).exists():
            zf.write(row.summary_path, arcname=f"{root}/LLM总结/总结.md")
    buf.seek(0)
    # 2026-08-20（走查实锤）：filename 直放中文 → HTTP 头 latin-1 编码 500；RFC 5987 filename* UTF-8 双轨
    zip_name = f"会议纪要_{safe_title}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 f"attachment; filename=\"meeting_{meeting_id[:8]}.zip\"; filename*=UTF-8''{urllib.parse.quote(zip_name)}"},
    )


@router.delete("/tools/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str, user: dict = Depends(require_custom_tool("meeting"))):
    """删除录音（后台任务随进程存活自然结束——DB 行已删后任务写库会跳过；目录清理）。"""
    import shutil

    row = await _get_meeting(meeting_id, user)
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM meeting_recordings WHERE id=:id"), {"id": meeting_id})
    shutil.rmtree(_meeting_dir(meeting_id), ignore_errors=True)
    return {"ok": True}
