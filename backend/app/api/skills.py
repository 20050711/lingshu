"""技能 API（四期重构：工具元数据 + 团队技能 SKILL.md）+ 用户工具勾选持久化。

- GET /skills        返回 {tools: 工具元数据列表, dept_skills: 本团队技能}（无关键词匹配，描述驱动）
- GET /skills/prefs  当前用户勾选 {enabled_ids, auto_skill}（无记录返回默认工具集）
- PUT /skills/prefs  保存勾选（整组替换；__auto__ 行存自动开关）
- GET /skills/files            团队技能列表（employee/dept_admin 本团队；admin 任意）
- POST /skills/files           上传 SKILL.md（multipart；dept_admin 本团队 / admin 任意团队）
- PUT /skills/files/{id}       更新或启停（同上传权限，按团队校验）
- DELETE /skills/files/{id}    删除（同上传权限）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from typing import Literal
from sqlalchemy import delete, insert, select, text

from app.agent.tools import get_all_tool_meta as get_tools_meta
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.logging import get_logger
from app.core.middleware import get_business_user, get_current_user, require_admin_or_dept_admin
from app.models import UserSkillPref

logger = get_logger("api.skills")

router = APIRouter(prefix="/skills", tags=["skills"])

# 四期重构：默认勾选工具集（原复合技能展开；run_script 兜底默认开启）
# 2026-09-17：sql_query 随数据查询线下线（取数改走资料文件 + file_parse/run_script）
DEFAULT_TOOLS = ["generate_chart", "file_parse", "doc_export", "run_script"]


@router.get("")
async def list_skills(user: dict = Depends(get_business_user)):
    from app.services.config_service import get_dept_tools
    from app.services.skill_file_service import list_active_skills

    dept_skills = await list_active_skills(user["dept_id"])
    # 2026-09-01：本团队工具黑名单（None=无禁用全部可用）——前端按此置灰被禁卡片
    dept_blocked = await get_dept_tools(user["dept_id"])
    return {
        "tools": get_tools_meta(),
        "dept_blocked": dept_blocked,
        "dept_skills": [
            {"id": s["id"], "name": s["name"], "description": s["description"], "tools": s["tools"]}
            for s in dept_skills
        ],
    }


@router.get("/prefs")
async def get_prefs(user: dict = Depends(get_business_user)):
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(UserSkillPref.skill_id, UserSkillPref.enabled).where(
                    UserSkillPref.user_id == user["user_id"]
                )
            )
        ).all()
    prefs = {r[0]: r[1] for r in rows}
    if not prefs:
        return {"enabled_ids": DEFAULT_TOOLS, "auto_skill": False}
    # 2026-08-21：过滤命名空间行（dept:/g: 为团队/全局技能个人偏好，不属于平台工具勾选；
    # 历史 bug：dept: 行混入 enabled_ids 会随 PUT /prefs 触发 E011）
    # 2026-09-17（工具下线）：已注销工具的历史勾选同样过滤——否则前端原样回传 → PUT 400
    #（实测 sql_query 下线后老用户在技能页怎么点都保存不了）
    registered = {t["id"] for t in get_tools_meta()}
    enabled_ids = [sid for sid, on in prefs.items()
                   if sid != "__auto__" and on and not sid.startswith(("dept:", "g:"))
                   and sid in registered]
    auto_skill = bool(prefs.get("__auto__", False))
    return {"enabled_ids": enabled_ids, "auto_skill": auto_skill}


class PrefsIn(BaseModel):
    enabled_ids: list[str] = []
    auto_skill: bool = False


@router.put("/prefs")
async def put_prefs(req: PrefsIn, user: dict = Depends(get_business_user)):
    """保存勾选：整组替换（DELETE 该用户全部行 + INSERT），避免逐行 upsert 竞争。"""
    # L16：enabled_ids 白名单校验（只接受已注册工具 id 与 __auto__，防任意字符串入库）
    # 2026-09-17（工具下线）：未注册 id **丢弃并记日志**，不再 400——下线工具后老用户偏好里
    # 必然残留旧 id（实测 sql_query），原「直接报错」让用户在技能页怎么点都保存不了；
    # 丢弃同样保证库里只出现已注册工具（安全属性不变）。
    valid_ids = {t["id"] for t in get_tools_meta()} | {"__auto__"}
    unknown = [sid for sid in req.enabled_ids if sid not in valid_ids]
    if unknown:
        logger.warning("skills/prefs 丢弃未注册 id: %s（工具可能已下线）", unknown[:5])
    enabled_ids = [sid for sid in req.enabled_ids if sid in valid_ids]
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(delete(UserSkillPref).where(UserSkillPref.user_id == user["user_id"]))
        for sid in enabled_ids:
            await conn.execute(
                insert(UserSkillPref).values(user_id=user["user_id"], skill_id=sid, enabled=True)
            )
        await conn.execute(
            insert(UserSkillPref).values(user_id=user["user_id"], skill_id="__auto__", enabled=req.auto_skill)
        )
    return {"ok": True}


class DeptPrefsIn(BaseModel):
    """员工个人团队技能启用集合（2026-08-21：与团队管理员启停取交集生效）。"""
    enabled_ids: list[int] = []


@router.get("/dept-prefs")
async def get_dept_prefs(user: dict = Depends(get_business_user)):
    """员工团队技能偏好：本团队技能列表 + 我的启用集合。

    configured=false = 未配置过偏好（全部启用，兼容现状）；enabled_ids 为空且 configured=true = 全关。
    """
    from app.services.skill_file_service import get_user_dept_skill_prefs

    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, skill_name, description, status, skill_dir FROM skill_files "
                     "WHERE dept_id=:d ORDER BY sort_order, id"),
                {"d": user["dept_id"]},
            )
        ).all()
    prefs = await get_user_dept_skill_prefs(user["user_id"])
    return {
        "skills": [
            {"id": r.id, "name": r.skill_name, "description": r.description,
             "status": r.status, "has_scripts": bool(r.skill_dir)}
            for r in rows
        ],
        "enabled_ids": sorted(prefs) if prefs is not None else [],
        "configured": prefs is not None,
    }


@router.put("/dept-prefs")
async def put_dept_prefs(req: DeptPrefsIn, user: dict = Depends(get_business_user)):
    """保存员工团队技能启用集合：整组替换 dept:* 前缀行（不动平台工具勾选/__auto__）。"""
    engine = get_global_engine()
    ids = sorted(set(req.enabled_ids))
    if ids:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id FROM skill_files WHERE dept_id=:d AND id = ANY(:ids)"),
                    {"d": user["dept_id"], "ids": ids},
                )
            ).all()
        if len(rows) != len(ids):
            raise app_error("E011", "包含不属于本团队的技能", status_code=400)
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM user_skill_prefs WHERE user_id=:uid AND skill_id LIKE :p"),
            {"uid": user["user_id"], "p": "dept:%"},
        )
        # 哨兵行：标记「已配置」（区分全关与未配置；get 侧据此返回 configured=true）
        await conn.execute(
            text("INSERT INTO user_skill_prefs (user_id, skill_id, enabled) VALUES (:uid, :sid, TRUE)"),
            {"uid": user["user_id"], "sid": "dept:__configured__"},
        )
        for sid in ids:
            await conn.execute(
                text("INSERT INTO user_skill_prefs (user_id, skill_id, enabled) VALUES (:uid, :sid, TRUE)"),
                {"uid": user["user_id"], "sid": f"dept:{sid}"},
            )
    return {"ok": True}


# ===== 全局技能（默认AI技能，运维发布）=====

class GlobalPrefsIn(BaseModel):
    """员工个人全局技能启用集合（2026-08-21：与运维启停、团队开关取交集生效）。"""
    enabled_ids: list[int] = []


@router.get("/global-prefs")
async def get_global_prefs(user: dict = Depends(get_business_user)):
    """员工全局技能偏好：全局技能列表 + 我的启用集合 + 团队开关状态（供前端置灰）。

    configured=false = 未配置过偏好（跟随团队开关）；enabled_ids 为空且 configured=true = 全关。
    dept_enabled=false = 团队管理员已关闭该技能（本团队不可用）；status!=active = 运维已停用。
    """
    from app.services.config_service import get_dept_global_skills
    from app.services.skill_file_service import get_user_global_skill_prefs

    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, skill_name, description, status, skill_dir FROM skill_files "
                     "WHERE dept_id='global' ORDER BY sort_order, id"),
            )
        ).all()
    prefs = await get_user_global_skill_prefs(user["user_id"])
    dept_whitelist = await get_dept_global_skills(user["dept_id"])
    return {
        "skills": [
            {"id": r.id, "name": r.skill_name, "description": r.description,
             "status": r.status, "has_scripts": bool(r.skill_dir),
             "dept_enabled": dept_whitelist is None or r.id in dept_whitelist}
            for r in rows
        ],
        "enabled_ids": sorted(prefs) if prefs is not None else [],
        "configured": prefs is not None,
    }


@router.put("/global-prefs")
async def put_global_prefs(req: GlobalPrefsIn, user: dict = Depends(get_business_user)):
    """保存员工全局技能启用集合：整组替换 g:* 前缀行（不动平台工具勾选/__auto__/dept: 行）。"""
    engine = get_global_engine()
    ids = sorted(set(req.enabled_ids))
    if ids:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id FROM skill_files WHERE dept_id='global' AND id = ANY(:ids)"),
                    {"ids": ids},
                )
            ).all()
        if len(rows) != len(ids):
            raise app_error("E011", "包含不属于全局技能清单的 id", status_code=400)
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM user_skill_prefs WHERE user_id=:uid AND skill_id LIKE :p"),
            {"uid": user["user_id"], "p": "g:%"},
        )
        # 哨兵行：标记「已配置」（区分全关与未配置；get 侧据此返回 configured=true）
        await conn.execute(
            text("INSERT INTO user_skill_prefs (user_id, skill_id, enabled) VALUES (:uid, :sid, TRUE)"),
            {"uid": user["user_id"], "sid": "g:__configured__"},
        )
        for sid in ids:
            await conn.execute(
                text("INSERT INTO user_skill_prefs (user_id, skill_id, enabled) VALUES (:uid, :sid, TRUE)"),
                {"uid": user["user_id"], "sid": f"g:{sid}"},
            )
    return {"ok": True}


class GlobalDeptIn(BaseModel):
    """团队对全局技能的白名单：[]=本团队全禁；非空=仅允许清单内；None=恢复全部允许。"""
    enabled_ids: list[int] | None = None


@router.get("/global-dept")
async def get_global_dept(dept_id: str | None = None, user: dict = Depends(require_admin_or_dept_admin)):
    """团队对全局技能的开关（2026-08-21）：dept_admin/ceo 仅本团队；admin 可带 dept_id 查任意团队。

    configured=false = 未配置（全部允许）；enabled_ids 为空且 configured=true = 全禁。
    """
    from app.services.config_service import get_dept_global_skills

    target = dept_id or user["dept_id"]
    if user["role"] in ("dept_admin", "ceo") and target != user["dept_id"]:
        raise app_error("E006", "无权查看其他团队配置", status_code=403)
    whitelist = await get_dept_global_skills(target)
    return {"dept_id": target, "enabled_ids": sorted(whitelist) if whitelist is not None else [],
            "configured": whitelist is not None}


@router.put("/global-dept")
async def put_global_dept(req: GlobalDeptIn, dept_id: str | None = None,
                          user: dict = Depends(require_admin_or_dept_admin)):
    """设置团队对全局技能的开关；admin 可带 dept_id 操作任意团队。"""
    from app.services.config_service import set_dept_global_skills

    target = dept_id or user["dept_id"]
    if user["role"] in ("dept_admin", "ceo") and target != user["dept_id"]:
        raise app_error("E006", "无权修改其他团队配置", status_code=403)
    ids = sorted(set(req.enabled_ids or []))
    if ids:
        # 校验 id 必须属于全局技能（防任意整数入库）
        engine = get_global_engine()
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT id FROM skill_files WHERE dept_id='global' AND id = ANY(:ids)"),
                    {"ids": ids},
                )
            ).all()
        if len(rows) != len(ids):
            raise app_error("E011", "包含不属于全局技能清单的 id", status_code=400)
    await set_dept_global_skills(target, req.enabled_ids)
    return {"ok": True, "dept_id": target}


# ===== 团队技能管理（SKILL.md）=====

async def _check_skill_dept(skill_id: int, user: dict) -> dict:
    """读取技能并校验团队权限：dept_admin 仅本团队；admin 任意。返回技能行。

    2026-08-21：全局技能（dept_id='global'）仅运维（admin）可管理（上传/启停/删除/重传）。
    """
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id, dept_id, skill_name FROM skill_files WHERE id=:id"), {"id": skill_id}
            )
        ).first()
    if row is None:
        raise app_error("E011", "技能不存在", status_code=404)
    if row.dept_id == "global" and user["role"] != "admin":
        raise app_error("E006", "全局技能仅运维可管理", status_code=403)
    if user["role"] in ("dept_admin", "ceo") and row.dept_id != user["dept_id"]:
        raise app_error("E006", "无权操作其他团队的技能", status_code=403)
    return {"id": row.id, "dept_id": row.dept_id, "name": row.skill_name}


@router.get("/files")
async def list_skill_files(dept_id: str | None = None, user: dict = Depends(get_current_user)):
    """技能列表：admin 可查任意团队（含 global 管理列表）；其余角色强制本团队。

    2026-08-21 修复：原依赖 get_business_user 在依赖层拦截 admin（"运维不参与业务问答"），
    admin 管理分支永远不可达——改为 get_current_user（admin 仅读列表，无安全影响）。
    """
    from app.services.skill_file_service import list_skills

    if user["role"] == "admin":
        # 2026-08-21：dept_id='global' 为全局技能管理列表（仅运维）
        if dept_id == "global":
            return {"skills": await list_skills("global")}
        return {"skills": await list_skills(dept_id)}
    # 非 admin：global 一律拒绝（防窥探运维板块）；其余强制本团队
    if dept_id == "global":
        raise app_error("E006", "无权查看全局技能管理列表", status_code=403)
    return {"skills": await list_skills(user["dept_id"])}


async def _parse_upload(file: UploadFile) -> tuple[str | None, dict, bool]:
    """上传分流：zip（根含 SKILL.md + 脚本）→ 解析解压；md → 解析文本。返回 (raw_md_bytes, parsed, is_zip)。"""
    from app.services.skill_file_service import parse_skill_md, parse_skill_zip

    content_bytes = await file.read()
    is_zip = (file.filename or "").lower().endswith(".zip") \
        or (file.content_type or "") in ("application/zip", "application/x-zip-compressed")
    if is_zip:
        parsed = parse_skill_zip(content_bytes)  # 内部校验大小/文件数/slip/SKILL.md 必在根
        return parsed["raw_md"], {**parsed["meta"], "files": parsed["files"]}, True
    if len(content_bytes) > 1024 * 1024:  # N7：SKILL.md 大小上限 1MB（纯指令文本足够）
        raise app_error("E003", "SKILL.md 文件超过 1MB 限制", status_code=400)
    from app.services.skill_file_service import decode_skill_text

    raw = decode_skill_text(content_bytes)  # 2026-08-21：UTF-8 优先、GBK 回退（Windows 编辑器）
    return content_bytes, parse_skill_md(raw), False


@router.post("/files")
async def upload_skill_file(
    file: UploadFile = File(...),
    dept_id: str | None = Form(None),
    scope: Literal["dept", "global"] = Form("dept"),
    user: dict = Depends(require_admin_or_dept_admin),
):
    """上传技能：团队技能（默认 scope=dept，dept_admin 本团队/admin 任意）或全局技能（scope=global，仅 admin）。"""
    from app.services.skill_file_service import create_skill, create_skill_with_files

    target_dept = dept_id or user["dept_id"]
    # 2026-08-21：全局技能（默认AI技能）仅运维上传
    if scope == "global":
        if user["role"] != "admin":
            raise app_error("E006", "仅运维可上传全局技能", status_code=403)
        target_dept = "global"
    if user["role"] in ("dept_admin", "ceo") and target_dept != user["dept_id"]:
        raise app_error("E006", "团队管理员仅可为本团队上传技能", status_code=403)
    try:
        raw_md, parsed, is_zip = await _parse_upload(file)
    except ValueError as e:
        raise app_error("E011", str(e), status_code=400)
    try:
        if is_zip:
            skill_id = await create_skill_with_files(
                target_dept, parsed["name"], parsed["description"], parsed["body"],
                raw_md, parsed.get("files"), parsed["tools"], by_user=user["user_id"],
            )
        else:
            # 纯 md：无磁盘文件（skill_dir=NULL）
            skill_id = await create_skill(target_dept, parsed["name"], parsed["description"],
                                          parsed["body"], parsed["tools"], by_user=user["user_id"])
    except ValueError as e:
        raise app_error("E011", str(e), status_code=400)
    return {"id": skill_id, "name": parsed["name"], "dept_id": target_dept,
            "has_scripts": is_zip and bool(parsed.get("files"))}


@router.post("/files/{skill_id}/reupload")
async def reupload_skill_file(skill_id: int, file: UploadFile = File(...),
                              user: dict = Depends(require_admin_or_dept_admin)):
    """重传技能文件（md 或 zip，原子替换旧文件；纯文本技能请用 PUT content 更新）。"""
    from app.services.skill_file_service import replace_skill_files

    await _check_skill_dept(skill_id, user)
    try:
        raw_md, parsed, _ = await _parse_upload(file)
    except ValueError as e:
        raise app_error("E011", str(e), status_code=400)
    try:
        await replace_skill_files(skill_id, raw_md, parsed.get("files"))
    except ValueError as e:
        raise app_error("E011", str(e), status_code=400)
    return {"ok": True, "name": parsed["name"], "has_scripts": bool(parsed.get("files"))}


class SkillFileUpdateIn(BaseModel):
    content: str | None = None      # 重新上传 SKILL.md 全文（重新解析）
    # E-06(API)：枚举校验（原任意字符串 → update_skill 抛 ValueError → 500 非信封）
    status: Literal["active", "disabled"] | None = None


@router.put("/files/{skill_id}")
async def update_skill_file(skill_id: int, req: SkillFileUpdateIn, user: dict = Depends(require_admin_or_dept_admin)):
    from app.services.skill_file_service import parse_skill_md, update_skill

    await _check_skill_dept(skill_id, user)
    fields: dict = {}
    if req.status is not None:
        fields["status"] = req.status
    if req.content is not None:
        try:
            parsed = parse_skill_md(req.content)
        except ValueError as e:
            raise app_error("E011", str(e), status_code=400)
        fields.update(name=parsed["name"], description=parsed["description"],
                      body=parsed["body"], tools=parsed["tools"])
    await update_skill(skill_id, by_user=user["user_id"], **fields)
    return {"ok": True}


@router.delete("/files/{skill_id}")
async def delete_skill_file(skill_id: int, user: dict = Depends(require_admin_or_dept_admin)):
    from app.services.skill_file_service import delete_skill

    await _check_skill_dept(skill_id, user)
    await delete_skill(skill_id)
    return {"ok": True}
