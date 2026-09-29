"""反馈 API（四期重构：细分类型 + 来源页面 + 截图 + 细到个人 + 告警推送）。

- POST /feedback（multipart）：feedback_type（细分类型）、page（来源页面）、content、contact + 截图文件
- 截图存 /data/uploads/feedback/{uuid}/（写盘 to_thread 防阻塞）
- 告警推送：带类型/页面/用户/截图标记；带截图优先（推送文案标注"优先查看"）
- 管理端处理在 /admin/feedback（admin.py）
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import insert

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.file_utils import read_limited, sanitize_filename
from app.core.middleware import get_current_user
from app.models import Feedback
from app.services.alert import notify

router = APIRouter(prefix="/feedback", tags=["feedback"])

_settings = get_settings()
ALLOWED_IMG = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_SHOTS = 4  # 单次最多 4 张截图


@router.post("")
async def submit_feedback(
    feedback_type: str = Form("其他"),
    page: str = Form(""),
    content: str = Form(...),
    contact: str | None = Form(None),
    screenshots: list[UploadFile] = File(default=[]),
    user: dict = Depends(get_current_user),
):
    if not content.strip():
        from app.core.exceptions import app_error

        raise app_error("E011", "请填写反馈内容", status_code=400)
    shots = screenshots[:MAX_SHOTS]

    # 截图写盘（to_thread 防阻塞；路径存相对 /data/uploads 便于管理端定位）
    saved: list[str] = []
    if shots:
        fb_dir = Path(_settings.upload_dir) / "feedback" / uuid.uuid4().hex[:12]
        fb_dir.mkdir(parents=True, exist_ok=True)
        for f in shots:
            name = sanitize_filename(f.filename, "shot")
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_IMG:
                continue
            try:  # M3：截图分块读取限流 5MB（原无大小上限）
                data = await read_limited(f, 5 * 1024 * 1024)
            except ValueError as e:
                raise app_error("E003", str(e), status_code=400)
            rel = f"feedback/{fb_dir.name}/{name}"
            target = fb_dir / name
            await asyncio.to_thread(target.write_bytes, data)
            saved.append(rel)

    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            insert(Feedback).values(
                department_id=user["dept_id"],
                content=content.strip(),
                contact=contact,
                user_id=user.get("user_id"),
                feedback_type=feedback_type[:80],
                page=page[:100] or None,
                screenshots=saved or None,
            )
        )

    # 告警推送：富文本结构化段落（标题 + 类型/提交人/页面/截图 + 内容，加粗重点）
    # 带截图标注"优先查看"（用户要求优先推送带截图的）
    paras: list = [
        [{"tag": "text", "text": f"类型：{feedback_type}"}],
        [{"tag": "text", "text": f"提交人：{user['username']}（{user['dept_id']}）"}],
    ]
    if page:
        paras.append([{"tag": "text", "text": f"来源页面：{page}"}])
    if saved:
        paras.append([{"tag": "text", "text": f"截图：{len(saved)} 张（优先查看）"}])
    paras.append([{"tag": "text", "text": f"用户反馈：{content.strip()[:500]}"}])
    await notify("📝 用户反馈", paragraphs=paras)
    return {"ok": True}


def _to_item(r) -> dict:
    return {
        "id": r.id, "content": r.content, "status": r.status, "reply": r.reply,
        "operator": r.operator, "feedback_type": r.feedback_type, "page": r.page,
        "screenshots": r.screenshots,
        "processed_at": r.processed_at.isoformat() if r.processed_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/my")
async def my_feedback(user: dict = Depends(get_current_user)):
    """我的反馈列表（反馈中心页）：本人全部记录，倒序。"""
    from sqlalchemy import select

    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(Feedback).where(Feedback.user_id == user.get("user_id"))
                .order_by(Feedback.created_at.desc()).limit(200)
            )
        ).all()
    return {"items": [_to_item(r) for r in rows]}


@router.post("/{fid}/revoke")
async def revoke_feedback(fid: int, user: dict = Depends(get_current_user)):
    """撤销自己的反馈（软撤销：status → revoked，记录保留可审计）。

    仅本人 + 状态为 待处理/处理中 可撤销；已回复（done）不可撤销。
    """
    from sqlalchemy import select, update

    from app.core.exceptions import app_error

    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(select(Feedback).where(Feedback.id == fid))).first()
        if row is None:
            raise app_error("E011", "反馈不存在", status_code=404)
        if row.user_id != user.get("user_id") and user.get("role") != "admin":
            # 2026-08-12（H2 修复）：admin 可代撤（管理后台「撤销」入口）；普通用户仍限本人
            raise app_error("E006", "只能撤销自己的反馈", status_code=403)
        if row.status not in ("new", "processing"):
            raise app_error("E011", "该反馈已处理，无法撤销（如需更正请联系管理员）", status_code=400)
        await conn.execute(update(Feedback).where(Feedback.id == fid).values(status="revoked"))
    return {"ok": True}


@router.get("/photo")
async def feedback_photo(fid: int, name: str, user: dict = Depends(get_current_user)):
    """反馈截图下载（JWT + 归属校验：仅本人或运维管理可看；name 必须在该反馈的截图清单内）。"""
    from fastapi.responses import FileResponse

    from sqlalchemy import select

    from app.core.exceptions import app_error

    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(select(Feedback).where(Feedback.id == fid))).first()
    if row is None:
        raise app_error("E011", "反馈不存在", status_code=404)
    if row.user_id != user.get("user_id") and user.get("role") != "admin":
        raise app_error("E006", "无权查看该反馈截图", status_code=403)
    shots = row.screenshots or []
    if name not in shots:
        raise app_error("E011", "截图不存在", status_code=404)
    p = Path(_settings.upload_dir) / name
    try:
        resolved = p.resolve()
    except OSError:
        raise app_error("E011", "截图文件缺失", status_code=404)
    # 纵深防御：解析后必须仍在 /data/uploads/feedback 内（防路径穿越名残留）
    fb_root = (Path(_settings.upload_dir) / "feedback").resolve()
    if not resolved.is_relative_to(fb_root) or not resolved.is_file():
        raise app_error("E011", "截图文件缺失", status_code=404)
    return FileResponse(str(resolved), media_type="image/png")
