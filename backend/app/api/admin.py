"""管理后台 API（仅 admin 角色；开发期固定 /admin，生产随机化前缀）。

模块：系统概览 / 用户管理 / 团队管理 / 记忆库管理 / 知识库 /
配置管理 / 数据备份 / 日志与监控 / 系统维护。敏感操作写 audit_log。
（2026-09-17：原「数据同步」模块随数据查询线下线删除）
"""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import func, insert, select, text, update

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.exceptions import app_error  # S6：补 import（原 5 处 raise app_error 未定义 → NameError 裸 500）
from app.core.file_utils import sanitize_err_text  # R6：错误回显路径脱敏
from app.core.logging import get_logger
from app.core.middleware import require_admin
from app.core.redis import redis_delete, redis_ping, redis_scan_delete
from app.core.security import hash_password, validate_password
from app.models import AuditLog, Feedback, Session, SystemConfig, User

logger = get_logger("api.admin")
_settings = get_settings()
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

# 团队信息单一事实源（三期 M12：departments 表 + 60s 缓存，替代 DEPT_NAMES 硬编码）
from app.services.dept_service import create_dept, dept_stats, drop_dept, get_dept_name, list_depts
# 2026-09-10：API 层直写 system_config 的写点必须顺手失效进程内配置缓存（否则最长 60s 读旧值）
from app.services.config_service import (get_upload_limits, invalidate_config_cache,
                                         set_upload_limits, upload_limit_mb, upload_limits_meta)

async def _audit(operator: str, action: str, target: str, detail: dict | None = None, ip: str = "") -> None:
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            insert(AuditLog).values(operator=operator, action=action, target=target, detail=detail, ip=ip)
        )


def _is_uuid(s: str) -> bool:
    """F5（红队二次）：UUID 格式校验（对齐 chat.py 同名函数——防 session_id 拼路径穿越）。"""
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


# ===== 系统概览 =====

@router.get("/overview")
async def overview(user: dict = Depends(require_admin)):
    engine = get_global_engine()
    async with engine.connect() as conn:
        user_count = (await conn.execute(select(func.count()).select_from(User))).scalar()
        session_count = (await conn.execute(select(func.count()).select_from(Session))).scalar()
        # 2026-09-17：last_sync（取自 sync_log）随数据查询线下线删除
        last_backup = None
        backup_dir = Path(_settings.backup_dir)
        if backup_dir.exists():
            dumps = sorted(backup_dir.glob("*.dump"), key=lambda f: f.stat().st_mtime, reverse=True)
            if dumps:
                last_backup = datetime.fromtimestamp(dumps[0].stat().st_mtime).isoformat()
    return {
        "services": {"db": "ok", "redis": "ok" if await redis_ping() else "down", "api": "ok"},  # M5：真实探活
        "metrics": {
            "user_count": user_count,
            "active_sessions": session_count,
            "last_backup_at": last_backup,
        },
    }


# ===== 用户管理 =====

class UserCreate(BaseModel):
    # E-09(API)：username/password 长度校验（原空密码可创建 → 任意人凭空密码登录；
    # username 超 String(50) → asyncpg DataError 500）
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)
    department_id: str
    role: Literal["employee", "dept_admin"] = "employee"  # L19：枚举校验（原任意字符串入库）


class UserRoleUpdate(BaseModel):
    role: Literal["employee", "dept_admin"]  # L19：同型


class UserStatusUpdate(BaseModel):
    status: Literal["active", "disabled"]  # L19：同型


class ResetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=6, max_length=100)  # E-09：同 UserCreate


@router.get("/users")
async def list_users(user: dict = Depends(require_admin)):
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(User).order_by(User.id))).all()
    return {
        "users": [
            {
                "id": r.id,
                "username": r.username,
                "role": r.role,
                "dept_id": r.department_id,
                "dept_name": await get_dept_name(r.department_id),
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post("/users")
async def create_user(req: UserCreate, user: dict = Depends(require_admin)):
    # 2026-09-01：密码规则统一 8-16 位（validate_password；与自助改密/reset-password 一致）
    pwd_err = validate_password(req.password)
    if pwd_err:
        raise app_error("E011", pwd_err, status_code=400)
    # 三期 M12：团队必须已注册（departments 表）；对应团队库不存在则自动建库（幂等）
    dept_row = None
    engine = get_global_engine()
    async with engine.connect() as conn:
        dept_row = (
            await conn.execute(text("SELECT 1 FROM departments WHERE dept_id=:d"), {"d": req.department_id})
        ).first()
    if dept_row is None:
        raise app_error("E011", f"团队 {req.department_id} 未注册，请先在团队管理注册", status_code=400)
    # M4（2026-08-12）：平台团队 ceo/dept_root 不创建业务库——ceo 实际用 tardis_ceo_db（backup.py:29
    # 唯一映射），dept_root 无业务库；无条件 create_dept 会建出无人使用的孤儿库 dept_ceo_db/dept_dept_root_db
    if req.department_id in ("ceo", "dept_root"):
        raise app_error("E011", f"平台团队 {req.department_id} 不支持新增用户（该团队用户由平台维护）", status_code=400)
    try:
        await create_dept(req.department_id, req.department_id)
    except ValueError as e:
        raise app_error("E011", f"团队库创建失败: {str(e)[:100]}", status_code=400)
    async with engine.begin() as conn:
        # 四期重构：账号团队内唯一（同用户名可在不同团队存在）
        existing = (
            await conn.execute(
                select(User.id).where(
                    User.department_id == req.department_id,
                    User.username == req.username,
                )
            )
        ).first()
        if existing:
            raise app_error("E011", "该团队下用户名已存在", status_code=400)
        await conn.execute(
            insert(User).values(
                username=req.username,
                password_hash=hash_password(req.password),
                department_id=req.department_id,
                role=req.role,
            )
        )
    await _audit(user["username"], "user.create", req.username, {"department_id": req.department_id})
    return {"ok": True}


@router.put("/users/{user_id}/status")
async def update_user_status(user_id: int, req: UserStatusUpdate, user: dict = Depends(require_admin)):
    # 2026-08-06 事故修复：原无任何保护——admin 禁用自己导致系统锁死（middleware 校验 status 即时生效）。
    # 防护：不能禁用自己；admin/ceo 预置账号不可禁用（唯一 admin 被禁则无人可恢复）。
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(select(User).where(User.id == user_id))).first()
        if row is None:
            raise app_error("E011", "用户不存在", status_code=404)
        if row.id == user["user_id"]:
            raise app_error("E011", "不能禁用当前登录账号", status_code=400)
        if row.role in ("admin", "ceo"):
            raise app_error("E011", "预置账号（运维管理/CEO）不可禁用", status_code=400)
        await conn.execute(update(User).where(User.id == user_id).values(status=req.status))
    await _audit(user["username"], "user.status", str(user_id), {"status": req.status})
    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, req: ResetPasswordRequest, user: dict = Depends(require_admin)):
    # 2026-09-01：密码规则统一 8-16 位（validate_password）
    pwd_err = validate_password(req.password)
    if pwd_err:
        raise app_error("E011", pwd_err, status_code=400)
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(update(User).where(User.id == user_id).values(password_hash=hash_password(req.password)))
    await _audit(user["username"], "user.reset_password", str(user_id))
    return {"ok": True}


@router.put("/users/{user_id}/role")
async def update_user_role(user_id: int, req: UserRoleUpdate, user: dict = Depends(require_admin)):
    """设置/取消团队管理员（四期重构）。拒绝改自己/admin/ceo；改角色后旧 JWT ≤7 天仍带旧 role，前端提示重新登录。"""
    if req.role not in ("employee", "dept_admin"):
        raise HTTPException(400, detail={"code": "E011", "message": "仅支持 employee / dept_admin 角色"})
    if user_id == user["user_id"]:
        raise HTTPException(400, detail={"code": "E011", "message": "不能修改自己的角色"})
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(select(User.role).where(User.id == user_id))).first()
        if row is None:
            # E-05(API)：E008 语义是"会话只读"——误用会污染错误码归因（同文件 :167 用 E011）
            raise HTTPException(404, detail={"code": "E011", "message": "用户不存在"})
        if row.role in ("admin", "ceo"):
            raise HTTPException(400, detail={"code": "E011", "message": "运维管理/CEO 角色不可变更"})
        await conn.execute(update(User).where(User.id == user_id).values(role=req.role))
    await _audit(user["username"], "user.role", str(user_id), {"role": req.role})
    return {"ok": True}


# 2026-09-17：/sync/logs、/sync/backups、/sync/rollback 三个接口随数据同步线下线删除
# （前端「数据同步」页一并删除；平台备份走 /admin/data 页的导出/导入）


# ===== 团队管理（三期 M12：注册=建库+建账号通道）=====

class DeptCreate(BaseModel):
    dept_id: str
    name: str


class DeptStatusUpdate(BaseModel):
    status: Literal["active", "disabled"]


class DeptToolsUpdate(BaseModel):
    tools: list[str]


class UserToolsUpdate(BaseModel):
    # 用户级工具白名单：勾选=允许（与团队级取交集最严生效）；空数组=恢复继承团队白名单
    tools: list[str] | None = None


@router.post("/departments")
async def admin_dept_create(req: DeptCreate, user: dict = Depends(require_admin)):
    try:
        result = await create_dept(req.dept_id, req.name)
    except ValueError as e:
        raise HTTPException(400, detail={"code": "E011", "message": str(e)[:200]})
    if result == "exists":
        raise HTTPException(400, detail={"code": "E011", "message": f"团队 {req.dept_id} 已存在"})
    await _audit(user["username"], "dept.create", req.dept_id, {"name": req.name})
    return {"ok": True, "result": result}


@router.get("/departments")
async def admin_depts():
    return {"departments": await dept_stats()}


@router.put("/departments/{dept_id}/status")
async def admin_dept_status(dept_id: str, req: DeptStatusUpdate, user: dict = Depends(require_admin)):
    """E-14(API，2026-08-10)：团队禁用/启用（原 departments.status 死字段——无任何接口可置非 active）。

    禁用校验：无 active 用户（有用户须先处理）。
    """
    if dept_id in ("dept_root", "ceo"):
        raise HTTPException(400, detail={"code": "E011", "message": "运维管理/CEO 团队不可禁用"})
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT dept_id FROM departments WHERE dept_id=:d"), {"d": dept_id})).first()
        if row is None:
            raise HTTPException(404, detail={"code": "E011", "message": "团队不存在"})
        if req.status == "disabled":
            active_users = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM users WHERE department_id=:d AND status='active'"),
                    {"d": dept_id},
                )
            ).scalar()
            if active_users:
                raise HTTPException(400, detail={"code": "E011",
                                                 "message": f"该团队还有 {active_users} 个启用中的账号，请先禁用/处理后再停用团队"})
        await conn.execute(
            text("UPDATE departments SET status=:s WHERE dept_id=:d"),
            {"s": req.status, "d": dept_id},
        )
    # 2026-09-17：_IMPORT_LOCKS 清理随导入管线下线删除（锁本身已不存在）
    await _audit(user["username"], "dept.status", dept_id, {"status": req.status})
    return {"ok": True}


@router.get("/dept-options")
async def admin_dept_options():
    """团队选项（含 CEO/运维管理；配置页等全量下拉使用——dept_stats 会跳过 ceo）。"""
    return {"departments": await list_depts()}


@router.delete("/departments/{dept_id}")
async def admin_dept_delete(dept_id: str, user: dict = Depends(require_admin)):
    """B4（2026-08-18）：删除团队（含全部用户与数据）。

    级联：用户（FK CASCADE → 会话/消息/反馈/记忆/批处理）+ 知识库 + 团队库 + 团队行。
    平台团队（运维管理/CEO）受保护；操作不可恢复，前端需二次确认。
    """
    try:
        await drop_dept(dept_id)
    except ValueError as e:
        raise app_error("E011", str(e), status_code=400)
    await _audit(user["username"], "dept.delete", dept_id, {})
    return {"ok": True}


@router.get("/departments/{dept_id}/tools")
async def admin_dept_tools(dept_id: str):
    """团队工具黑名单（2026-09-01 白名单→黑名单；缺省=None 全部可用）。存 system_config key=dept_block.{dept_id}（JSON 数组=禁用清单；存量 dept_tools.* 作废）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT value FROM system_config WHERE key=:k"), {"k": f"dept_block.{dept_id}"}
            )
        ).first()
    import json as _json

    if row and row[0]:
        try:
            tools = _json.loads(row[0])  # M6：配置写坏不得 500，回落空禁用清单
        except ValueError:
            logger.warning("dept_block.%s 配置不是合法 JSON，按空禁用清单处理", dept_id)
            tools = []
    else:
        tools = []
    return {"dept_id": dept_id, "tools": tools}


@router.put("/departments/{dept_id}/tools")
async def admin_dept_tools_put(dept_id: str, req: DeptToolsUpdate, user: dict = Depends(require_admin)):
    """写入团队工具黑名单（2026-09-01：勾选=禁用；空数组/删除 key=全部可用；key=dept_block.{dept_id}）。"""
    import json as _json

    engine = get_global_engine()
    key = f"dept_block.{dept_id}"
    if req.tools:
        value = _json.dumps(req.tools, ensure_ascii=False)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO system_config (key, value, description, updated_at) VALUES (:k, :v, :d, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"
                ),
                {"k": key, "v": value, "d": f"团队 {dept_id} 工具黑名单（2026-09-01 黑名单改造）"},
            )
    else:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
    invalidate_config_cache()   # 工具黑名单立即生效（admin 直写 system_config，不经 config_service setter）
    await _audit(user["username"], "dept.tools_block", dept_id, {"tools": req.tools})
    return {"ok": True}


@router.get("/users/{user_id}/tools")
async def admin_user_tools(user_id: int):
    """用户级工具黑名单（2026-09-01 白名单→黑名单：勾选=禁用）；tools=None=未配置（无禁用）。"""
    from app.services.config_service import get_user_tools

    tools = await get_user_tools(user_id)
    return {"user_id": user_id, "tools": tools}


@router.put("/users/{user_id}/tools")
async def admin_user_tools_put(user_id: int, req: UserToolsUpdate, user: dict = Depends(require_admin)):
    """写入用户级工具黑名单（2026-09-01：勾选=禁用，与团队黑名单并集——任一禁用即禁用；空数组=无禁用；
    key=user_block.{user_id}，存量 user_tools.* 作废）。

    含 run_script 控制：用户级清单含 run_script → 该用户禁用 run_script（未配置用户级清单时仍恒注入兜底）。
    """
    import json as _json

    engine = get_global_engine()
    key = f"user_block.{user_id}"
    if req.tools:
        value = _json.dumps(req.tools, ensure_ascii=False)
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO system_config (key, value, description, updated_at) VALUES (:k, :v, :d, NOW()) "
                     "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"),
                {"k": key, "v": value, "d": f"用户 {user_id} 工具黑名单（与团队级并集生效）"},
            )
    else:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
    invalidate_config_cache()   # 用户级工具黑名单立即生效（同团队级）
    await _audit(user["username"], "user.tools_block", str(user_id), {"tools": req.tools})
    return {"ok": True}


@router.get("/tools-meta")
async def admin_tools_meta():
    """4.1：平台工具动态元数据（admin 白名单 UI 渲染——不硬编码工具名，运维看中文名/图标/分组）。"""
    from app.agent.skill_registry import get_all_skills

    metas = get_all_skills()
    return {"tools": [
        {k: m.get(k) for k in ("id", "name", "icon", "summary", "group", "select_mode", "status")}
        for m in metas
    ]}


@router.get("/custom-tools")
async def admin_custom_tools_meta(user: dict = Depends(require_admin)):
    """定制化工具注册表（2026-08-24：前端禁用勾选动态加载，替代前端硬编码——后续新增工具只改后端）。"""
    from app.api.tools_guard import CUSTOM_TOOLS

    return {"tools": [{"id": k, "name": v} for k, v in CUSTOM_TOOLS.items()]}


@router.get("/departments/{dept_id}/custom-tools")
async def admin_dept_custom_tools(dept_id: str):
    """团队定制化工具黑名单（2026-09-01 白名单→黑名单：缺省=None 全部可用；返回禁用清单）。"""
    from app.api.tools_guard import get_custom_tools

    return {"dept_id": dept_id, "tools": await get_custom_tools(dept_id)}


class CustomToolsUpdate(BaseModel):
    tools: list[str]


# ===== 团队定制化工具白名单（2026-09-02：/dtools 板块，默认全团队关闭）=====

@router.get("/dept-custom-tools")
async def admin_dept_custom_tools_meta(user: dict = Depends(require_admin)):
    """团队定制化工具注册表（白名单勾选动态加载）。"""
    from app.api.tools_guard import DEPT_CUSTOM_TOOLS

    return {"tools": [{"id": k, "name": v} for k, v in DEPT_CUSTOM_TOOLS.items()]}


@router.get("/departments/{dept_id}/dept-tools")
async def admin_dept_tools(dept_id: str):
    """团队定制化工具白名单（已开通清单；缺省 None=全部关闭）。"""
    from app.api.tools_guard import get_dept_custom_tools

    return {"dept_id": dept_id, "tools": await get_dept_custom_tools(dept_id)}


@router.put("/departments/{dept_id}/dept-tools")
async def admin_dept_tools_put(dept_id: str, req: CustomToolsUpdate, user: dict = Depends(require_admin)):
    """写入团队定制化工具白名单（勾选=开通；空数组/删除 key=全部关闭——默认态）。
    2026-09-02：与 custom_block 黑名单并存，最严生效。"""
    import json as _json

    from app.api.tools_guard import DEPT_CUSTOM_TOOLS

    unknown = [t for t in req.tools if t not in DEPT_CUSTOM_TOOLS]
    if unknown:
        raise HTTPException(400, detail={"code": "E011", "message": f"未知团队定制化工具: {unknown}"})
    engine = get_global_engine()
    key = f"custom_allow.{dept_id}"
    if req.tools:
        value = _json.dumps(req.tools, ensure_ascii=False)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO system_config (key, value, description, updated_at) VALUES (:k, :v, :d, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"
                ),
                {"k": key, "v": value, "d": f"团队 {dept_id} 团队定制化工具白名单（2026-09-02 白名单制）"},
            )
    else:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
    await _audit(user["username"], "dept.custom_allow", dept_id, {"tools": req.tools})
    return {"ok": True}


@router.put("/departments/{dept_id}/custom-tools")
async def admin_dept_custom_tools_put(dept_id: str, req: CustomToolsUpdate, user: dict = Depends(require_admin)):
    """写入团队定制化工具黑名单（勾选=禁用；空数组/删除 key=全部可用）。
    2026-09-01：白名单→黑名单改造，key 从 custom_tools.{dept} 换为 custom_block.{dept}（存量作废=默认全开）。"""
    import json as _json

    from app.api.tools_guard import CUSTOM_TOOLS

    unknown = [t for t in req.tools if t not in CUSTOM_TOOLS]
    if unknown:
        raise HTTPException(400, detail={"code": "E011", "message": f"未知定制化工具: {unknown}"})
    engine = get_global_engine()
    key = f"custom_block.{dept_id}"
    if req.tools:
        value = _json.dumps(req.tools, ensure_ascii=False)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO system_config (key, value, description, updated_at) VALUES (:k, :v, :d, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"
                ),
                {"k": key, "v": value, "d": f"团队 {dept_id} 定制化工具黑名单（2026-09-01 黑名单改造）"},
            )
    else:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
    await _audit(user["username"], "dept.custom_block", dept_id, {"tools": req.tools})
    return {"ok": True}


# ===== 知识库管理（二期：上传/删除/分类）=====

class KbCategoryIn(BaseModel):
    # E-12(API，2026-08-10)：name 非空 + 长度上限（原空名/超长可入库，前端树渲染异常）
    name: str = Field(..., min_length=1, max_length=100)
    parent_id: int | None = None
    dept_id: str | None = None  # 三期 M14：NULL=全局分类；非空=团队分类


class KbDocRecategorizeIn(BaseModel):
    """admin 文档改分类（归属三态 2026-08-24）：仅 category_id，其他字段不可动。"""
    category_id: int | None = None


@router.get("/knowledge/categories")
async def kb_admin_categories():
    # 归属三态（2026-08-24）：admin 不管个人分类（user_id IS NULL 过滤）
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, name, parent_id, dept_id, sort_order FROM kb_categories "
                     "WHERE user_id IS NULL ORDER BY sort_order, id")
            )
        ).all()
    return {"categories": [{"id": r.id, "name": r.name, "parent_id": r.parent_id, "dept_id": r.dept_id} for r in rows]}


@router.post("/knowledge/categories")
async def kb_admin_category_create(req: KbCategoryIn, user: dict = Depends(require_admin)):
    # E-12(API)：parent 存在性校验（原畸形 parent_id → FK IntegrityError → 500 非信封）+ dept 已注册
    engine = get_global_engine()
    async with engine.begin() as conn:
        if req.parent_id is not None:
            p = (await conn.execute(text("SELECT 1 FROM kb_categories WHERE id=:id"), {"id": req.parent_id})).first()
            if p is None:
                raise HTTPException(400, detail={"code": "E011", "message": "父分类不存在"})
        if req.dept_id:
            d = (await conn.execute(text("SELECT 1 FROM departments WHERE dept_id=:d"), {"d": req.dept_id})).first()
            if d is None:
                raise HTTPException(400, detail={"code": "E011", "message": f"团队 {req.dept_id} 未注册"})
        new_id = (
            await conn.execute(
                text("INSERT INTO kb_categories (name, parent_id, dept_id, sort_order) VALUES (:n, :p, :d, 0) RETURNING id"),
                {"n": req.name, "p": req.parent_id, "d": req.dept_id},
            )
        ).scalar()
    await _audit(user["username"], "kb.category.create", req.name)
    return {"id": new_id}


@router.put("/knowledge/categories/{cat_id}")
async def kb_admin_category_update(cat_id: int, req: KbCategoryIn, user: dict = Depends(require_admin)):
    """分类重命名/调整归属（增删改查的"改"）。

    D26（2026-08-10）：分类改团队时同事务联动文档 department_id（原只改分类——
    出现"团队 A 分类下挂团队 B 文档"错位）；E-12：parent 存在性/自引用环/dept 已注册校验。
    """
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT name FROM kb_categories WHERE id=:id"), {"id": cat_id})).first()
        if row is None:
            raise HTTPException(404, detail={"code": "E011", "message": "分类不存在"})
        # E-12：parent 存在性 + 自引用环
        if req.parent_id is not None:
            p = (await conn.execute(text("SELECT 1 FROM kb_categories WHERE id=:id"), {"id": req.parent_id})).first()
            if p is None:
                raise HTTPException(400, detail={"code": "E011", "message": "父分类不存在"})
            if req.parent_id == cat_id:
                raise HTTPException(400, detail={"code": "E011", "message": "父分类不能是自身"})
        # E-12：dept_id 必须为已注册团队（原任意字符串产出对全员不可见的幽灵分类）
        if req.dept_id:
            d = (await conn.execute(text("SELECT 1 FROM departments WHERE dept_id=:d"), {"d": req.dept_id})).first()
            if d is None:
                raise HTTPException(400, detail={"code": "E011", "message": f"团队 {req.dept_id} 未注册"})
        old_dept = (await conn.execute(text("SELECT dept_id FROM kb_categories WHERE id=:id"), {"id": cat_id})).first()
        await conn.execute(
            text("UPDATE kb_categories SET name=:n, dept_id=:d WHERE id=:id"),
            {"n": req.name, "d": req.dept_id, "id": cat_id},
        )
        # D26：分类改挂团队 → 其下文档团队归属联动迁移（改回全局 → NULL）
        # 归属三态（2026-08-24）：必须加 AND user_id IS NULL——防个人文档被拽进团队（越权）
        if old_dept is not None and old_dept[0] != req.dept_id:
            await conn.execute(
                text("UPDATE kb_documents SET department_id=:d WHERE category_id=:id AND user_id IS NULL"),
                {"d": req.dept_id, "id": cat_id},
            )
    await _audit(user["username"], "kb.category.update", f"{cat_id}:{row.name}→{req.name}", {"dept_id": req.dept_id})
    # 2026-09-10（缓存核查）：改团队会联动改文档归属 → 双向可见性都会错位，必须清检索缓存
    from app.services.kb_service import clear_kb_match_cache

    await clear_kb_match_cache()
    return {"ok": True}


@router.delete("/knowledge/categories/{cat_id}")
async def kb_admin_category_delete(cat_id: int, user: dict = Depends(require_admin)):
    """分类删除：有子分类拒绝；其下文档分类置空（保留文档）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT name FROM kb_categories WHERE id=:id"), {"id": cat_id})).first()
        if row is None:
            raise HTTPException(404, detail={"code": "E011", "message": "分类不存在"})
        child = (await conn.execute(text("SELECT 1 FROM kb_categories WHERE parent_id=:id LIMIT 1"), {"id": cat_id})).first()
        if child:
            raise HTTPException(400, detail={"code": "E011", "message": "该分类存在子分类，请先删除子分类"})
        await conn.execute(text("UPDATE kb_documents SET category_id=NULL WHERE category_id=:id"), {"id": cat_id})
        await conn.execute(text("DELETE FROM kb_categories WHERE id=:id"), {"id": cat_id})
    await _audit(user["username"], "kb.category.delete", f"{cat_id}:{row.name}")
    from app.services.kb_service import clear_kb_match_cache   # 2026-09-10 同类排查：删分类后缓存里仍有旧分类名

    await clear_kb_match_cache()
    return {"ok": True}


@router.post("/knowledge/documents")
async def kb_admin_document_create(
    file: UploadFile = File(...),
    category_id: int | None = Form(None),
    title: str = Form(""),
    department_id: str | None = Form(None),
    user: dict = Depends(require_admin),
):
    """admin 上传知识文档（归属三态 2026-08-24）：department_id=NULL=全局文档（所有团队可检索）；
    指定团队=该团队文档（仅本团队可见）。分类校验按目标团队（全局分类或目标团队分类），个人分类一律拒绝。"""
    from app.services.kb_service import upload_kb_document

    if category_id is not None:
        engine = get_global_engine()
        async with engine.connect() as conn:
            cat = (
                await conn.execute(
                    text("SELECT dept_id, user_id FROM kb_categories WHERE id=:id"), {"id": category_id}
                )
            ).first()
        # 个人分类一律拒绝；分类归属须匹配：目标团队=该分类归属，或该分类为全局分类（可挂任意目标团队文档）
        if cat is None or cat[1] is not None:
            raise app_error("E011", "分类不存在或为个人分类", status_code=400)
        if cat[0] is not None and (cat[0] != department_id or department_id is None):
            raise app_error("E011", f"分类属于团队 {cat[0]}，与目标团队 {department_id or '全局'} 不匹配",
                            status_code=400)

    fname = file.filename or "doc"
    content = await file.read()
    if len(content) > await upload_limit_mb("kb_mb") * 1024 * 1024:  # N7：KB 上限（2026-09-16 起动态，管理端可调）
        raise app_error("E003", f"文件超过 {await upload_limit_mb('kb_mb')}MB 限制", status_code=400)
    doc_id = await upload_kb_document(fname, content, title, category_id,
                                      department_id=department_id, uploaded_by=user["user_id"])
    await _audit(user["username"], "kb.document.create", fname, {"department_id": department_id})
    return {"id": doc_id, "title": title.strip() or fname}


@router.put("/knowledge/documents/{doc_id}/category")
async def kb_admin_document_recategorize(doc_id: int, req: KbDocRecategorizeIn,
                                         user: dict = Depends(require_admin)):
    """admin 文档改分类（归属三态 2026-08-24，管理端专用——业务端 PUT 拒绝运维账号）。

    规则：全局文档→全局分类或 NULL；团队文档→全局或本团队分类；个人文档→403（个人库 admin 只读+删）。
    """
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT department_id, user_id FROM kb_documents WHERE id=:id"), {"id": doc_id}
            )
        ).first()
        if row is None:
            raise HTTPException(404, detail={"code": "E011", "message": "文档不存在"})
        if row.user_id is not None:
            raise app_error("E006", "个人知识库文档仅本人可管理（admin 只读+删）", status_code=403)
        if req.category_id is not None:
            cat = (
                await conn.execute(
                    text("SELECT dept_id, user_id FROM kb_categories WHERE id=:id"), {"id": req.category_id}
                )
            ).first()
            if cat is None or cat[1] is not None:
                raise app_error("E011", "分类不存在或为个人分类", status_code=400)
            # 团队分类只能挂本团队文档；全局文档不能挂团队分类（其余团队不可见）
            if cat[0] is not None and (row.department_id is None or cat[0] != row.department_id):
                raise app_error("E011", f"分类属于团队 {cat[0]}，与文档归属（{row.department_id or '全局'}）不匹配",
                                status_code=400)
        await conn.execute(
            text("UPDATE kb_documents SET category_id=:cid, updated_at=NOW() WHERE id=:id"),
            {"cid": req.category_id, "id": doc_id},
        )
    await _audit(user["username"], "kb.document.recategorize", f"doc {doc_id}", {"category_id": req.category_id})
    from app.services.kb_service import clear_kb_match_cache   # 同上：改挂分类后缓存 category 字段过期

    await clear_kb_match_cache()
    return {"ok": True}


@router.get("/knowledge/documents")
async def kb_admin_documents(user: dict = Depends(require_admin)):
    """知识文档列表（admin 管理用，含团队列与上传人；业务端检索走 /knowledge/search）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT d.id, d.title, d.category_id, c.name AS category_name, d.department_id, "
                     "d.file_type, d.file_size, d.summary, d.created_at, d.uploaded_by, u.username, d.user_id "
                     "FROM kb_documents d LEFT JOIN kb_categories c ON c.id = d.category_id "
                     "LEFT JOIN users u ON u.id = d.uploaded_by "
                     "ORDER BY d.id DESC LIMIT 500")
            )
        ).all()
    return {"documents": [
        {"id": r.id, "title": r.title, "category_id": r.category_id, "category_name": r.category_name,
         "department_id": r.department_id, "file_type": r.file_type, "file_size": r.file_size,
         "summary": r.summary, "created_at": str(r.created_at),
         "uploaded_by": r.uploaded_by, "uploader": r.username, "user_id": r.user_id}
        for r in rows
    ]}


@router.get("/knowledge/documents/{doc_id}/detail")
async def kb_admin_document_detail(doc_id: int, user: dict = Depends(require_admin)):
    """知识文档详情（admin）：元信息 + 上传人 + 分段内容全文（kb_chunks 按序拼接）。
    路径带 /detail 后缀——避免与业务端 /knowledge/documents/{doc_id}（CT 工具读全文）路由冲突。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT d.id, d.title, d.summary, d.department_id, d.file_type, d.file_size, "
                     "d.created_at, d.uploaded_by, u.username, d.status "
                     "FROM kb_documents d LEFT JOIN users u ON u.id = d.uploaded_by WHERE d.id=:id"),
                {"id": doc_id},
            )
        ).first()
        if row is None:
            raise app_error("E011", "文档不存在", status_code=404)
        # E-04(数据层，2026-08-10)：列名修复——kb_chunks 实际为 document_id/seq/block_title
        # （原 chunk_index/doc_id 不存在 → 详情接口恒 500）
        chunks = (
            await conn.execute(
                text("SELECT seq, block_title, content FROM kb_chunks WHERE document_id=:id ORDER BY seq"),
                {"id": doc_id},
            )
        ).all()
    content = "\n\n".join(f"[{c.seq}] {c.content}" for c in chunks)
    return {
        "id": row.id, "title": row.title, "summary": row.summary,
        "department_id": row.department_id, "file_type": row.file_type, "file_size": row.file_size,
        "created_at": str(row.created_at), "uploaded_by": row.uploaded_by, "uploader": row.username,
        "status": row.status, "chunk_count": len(chunks), "content": content[:100000],
    }


@router.delete("/knowledge/documents/{doc_id}")
async def kb_admin_document_delete(doc_id: int, user: dict = Depends(require_admin)):
    engine = get_global_engine()
    file_path: str | None = None
    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT title, file_path FROM kb_documents WHERE id=:id"), {"id": doc_id})
        ).first()
        if row is None:
            raise app_error("E011", "文档不存在", status_code=404)
        file_path = row.file_path
        await conn.execute(text("DELETE FROM kb_documents WHERE id=:id"), {"id": doc_id})
    await _audit(user["username"], "kb.document.delete", row.title)
    # KB-REDESIGN：级联删盘（路径来自 DB 非用户输入；to_thread 防阻塞）
    if file_path:
        await asyncio.to_thread(Path(file_path).unlink, missing_ok=True)
    from app.services.kb_service import clear_kb_match_cache   # 2026-09-10 同类排查：已删文档的块不能再被检索命中

    await clear_kb_match_cache()
    return {"ok": True}


# ===== 记忆库管理（二期 M9：真实数据 + 共识阈值 + CEO 记忆 + 导出导入）=====
# 注意：/memory/thresholds 静态路径必须先于 /memory/{mem_id} 注册，避免路径参数抢先匹配

class ThresholdsIn(BaseModel):
    breadth_min_clients: int | None = None
    strength_min_rounds: int | None = None
    strength_min_clients: int | None = None
    window_days: int | None = None
    candidate_ttl_days: int | None = None
    max_active_per_dept: int | None = None


@router.get("/memory")
async def memory_list():
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, dept_id, category, content, status, client_ids, round_count, "
                     "first_seen_at, last_seen_at, activated_at FROM department_memory "
                     "ORDER BY status='active' DESC, updated_at DESC LIMIT 200")
            )
        ).all()
    return {
        "items": [
            {
                "id": r.id, "dept_id": r.dept_id, "category": r.category, "content": r.content,
                "status": r.status, "client_ids": r.client_ids, "round_count": r.round_count,
                "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
                "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
                "activated_at": r.activated_at.isoformat() if r.activated_at else None,
            }
            for r in rows
        ]
    }


@router.get("/memory/thresholds")
async def memory_thresholds():
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM memory_thresholds WHERE id=1"))).first()
    if row is None:
        return {"thresholds": None, "note": "尚未初始化（默认值：广度3/强度5轮2端/窗口7天/候选TTL30天）"}
    return {
        "thresholds": {
            "breadth_min_clients": row.breadth_min_clients,
            "strength_min_rounds": row.strength_min_rounds,
            "strength_min_clients": row.strength_min_clients,
            "window_days": row.window_days,
            "candidate_ttl_days": row.candidate_ttl_days,
            "max_active_per_dept": row.max_active_per_dept,
        }
    }


@router.put("/memory/thresholds")
async def memory_thresholds_put(req: ThresholdsIn, user: dict = Depends(require_admin)):
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT id FROM memory_thresholds WHERE id=1"))).first()
        if row is None:
            await conn.execute(text("INSERT INTO memory_thresholds (id) VALUES (1)"))
        sets, params = [], {}
        for k in ("breadth_min_clients", "strength_min_rounds", "strength_min_clients",
                  "window_days", "candidate_ttl_days", "max_active_per_dept"):
            v = getattr(req, k)
            if v is not None:
                sets.append(f"{k}=:{k}")
                params[k] = v
        if sets:
            await conn.execute(
                text(f"UPDATE memory_thresholds SET {', '.join(sets)}, updated_at=NOW() WHERE id=1"),
                params,
            )
    await _audit(user["username"], "memory.thresholds", str(req.model_dump()))
    return {"ok": True}


@router.post("/memory/{mem_id}/activate")
async def memory_activate(mem_id: int, user: dict = Depends(require_admin)):
    """强制激活候选记忆（并替换同 category 旧 active）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT dept_id, category, content FROM department_memory WHERE id=:id"), {"id": mem_id})
        ).first()
        if row is None:
            raise app_error("E011", "记忆不存在", status_code=404)
        await conn.execute(
            text("UPDATE department_memory SET status='replaced', updated_at=NOW() "
                 "WHERE dept_id=:d AND category=:c AND status='active' AND id != :id"),
            {"d": row.dept_id, "c": row.category, "id": mem_id},
        )
        await conn.execute(
            text("UPDATE department_memory SET status='active', activated_at=NOW(), updated_at=NOW() WHERE id=:id"),
            {"id": mem_id},
        )
    await _audit(user["username"], "memory.activate", f"#{mem_id} {row.content[:50]}")
    return {"ok": True}


@router.delete("/memory/{mem_id}")
async def memory_delete(mem_id: int, user: dict = Depends(require_admin)):
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT content FROM department_memory WHERE id=:id"), {"id": mem_id})).first()
        if row is None:
            raise app_error("E011", "记忆不存在", status_code=404)
        await conn.execute(text("DELETE FROM department_memory WHERE id=:id"), {"id": mem_id})
    await _audit(user["username"], "memory.delete", f"#{mem_id} {row.content[:50]}")
    return {"ok": True}


class MemoryEditIn(BaseModel):
    content: str


@router.put("/memory/{mem_id}")
async def memory_edit(mem_id: int, req: MemoryEditIn, user: dict = Depends(require_admin)):
    """编辑记忆内容（重新计算 content_hash 聚合键）。"""
    engine = get_global_engine()
    content = req.content.strip()
    if not content:
        raise app_error("E011", "内容不能为空", status_code=400)
    from app.services.memory_service import content_hash

    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT content FROM department_memory WHERE id=:id"), {"id": mem_id})).first()
        if row is None:
            raise app_error("E011", "记忆不存在", status_code=404)
        await conn.execute(
            text("UPDATE department_memory SET content=:c, content_hash=:h, updated_at=NOW() WHERE id=:id"),
            {"c": content, "h": content_hash(content), "id": mem_id},
        )
    await _audit(user["username"], "memory.edit", f"#{mem_id}")
    return {"ok": True}


@router.post("/memory/scan")
async def memory_scan_now(user: dict = Depends(require_admin)):
    """手动触发候选共识扫描（等价每小时 job）。"""
    from app.services.memory_service import scan_candidates

    stats = await scan_candidates()
    return {"ok": True, **stats}


@router.get("/memory/export")
async def memory_export(user: dict = Depends(require_admin)):
    from fastapi.responses import JSONResponse

    from app.services.memory_service import export_all

    data = await export_all()
    await _audit(user["username"], "memory.export", "")
    return JSONResponse(content=data, headers={"Content-Disposition": 'attachment; filename="memory_export.json"'})


class MemoryImportIn(BaseModel):
    data: dict


@router.post("/memory/import")
async def memory_import(req: MemoryImportIn, user: dict = Depends(require_admin)):
    from app.services.memory_service import import_all

    imported = await import_all(req.data)
    await _audit(user["username"], "memory.import", json.dumps(imported))
    return {"ok": True, **imported}


# ---- 个人记忆管理（四期重构：全员个人记忆 /memory/users/*）----

@router.get("/memory/users/{user_id}")
async def user_memory_list(user_id: int, user: dict = Depends(require_admin)):
    from app.services.memory_service import user_list

    return {"items": await user_list(user_id)}


class UserMemoryIn(BaseModel):
    mem_type: str = "knowledge"
    content: str


@router.post("/memory/users/{user_id}")
async def user_memory_add(user_id: int, req: UserMemoryIn, user: dict = Depends(require_admin)):
    from app.services.memory_service import user_add

    mem_id = await user_add(user_id, req.mem_type, req.content)
    await _audit(user["username"], "memory.user.add", f"user={user_id}")
    return {"id": mem_id}


@router.delete("/memory/users/items/{mem_id}")
async def user_memory_delete(mem_id: int, user: dict = Depends(require_admin)):
    ok = await _user_delete_admin(mem_id)
    await _audit(user["username"], "memory.user.delete", f"#{mem_id}")
    return {"ok": ok}


async def _user_delete_admin(mem_id: int) -> bool:
    engine = get_global_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text("DELETE FROM user_memory WHERE id=:id"), {"id": mem_id})
    return result.rowcount > 0


# ===== 配置管理 =====

class ConfigPut(BaseModel):
    value: str


class UploadLimitsPut(BaseModel):
    values: dict[str, int]     # 只提交改动项；键名/文案由后端 upload_limits_meta() 下发


@router.get("/upload-limits")
async def upload_limits_get():
    """各入口上传大小上限（管理端「配置管理 → 上传限制」；值=MB，默认取 config.py）。"""
    limits = await get_upload_limits()
    return {"items": [{**m, "value": limits[m["key"]]} for m in upload_limits_meta()]}


@router.put("/upload-limits")
async def upload_limits_put(req: UploadLimitsPut):
    """保存上传大小上限（校验区间 → 写 system_config.upload_limits → 立即失效缓存）。"""
    await set_upload_limits(req.values)
    limits = await get_upload_limits()
    await _audit("admin", "config.upload_limits", "upload_limits",
                 {k: v for k, v in req.values.items()})
    return {"ok": True, "items": [{**m, "value": limits[m["key"]]} for m in upload_limits_meta()]}


@router.get("/config")
async def config_list():
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(SystemConfig).order_by(SystemConfig.key))).all()
    return {
        "configs": [{"key": r.key, "value": r.value, "description": r.description} for r in rows]
    }


@router.put("/config/{key}")
async def config_put(key: str, req: ConfigPut, user: dict = Depends(require_admin)):
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO system_config (key, value, updated_at) VALUES (:k, :v, NOW())"
                " ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = NOW()"
            ),
            {"k": key, "v": req.value},
        )
    # 2026-09-17：原 E-12 的 sql_cache:dept_root:* 清理随 sql_query 下线删除（该缓存已无写入方）
    # 2026-09-10：本接口能改任意 system_config（model_layer.* / 沙盒联网开关 / …）——
    # 进程内 kv 缓存必须同失效，否则改完最长 60s agent 仍按旧配置跑
    invalidate_config_cache()
    await _audit(user["username"], "config.update", key)
    return {"ok": True}


@router.get("/model-catalog")
async def model_catalog(user: dict = Depends(require_admin)):
    """模型候选目录（B4 配置：LLM 对话 / LLM 辅助任务 / 视觉识别 / 多模态生成；候选与任务由后端维护，前端不硬编码）。"""
    from app.services.model_catalog import AUX_TASKS, KIND_LABELS, get_model_catalog

    return {"kinds": list(KIND_LABELS.keys()), "labels": KIND_LABELS, "catalog": get_model_catalog(),
            "aux_tasks": AUX_TASKS,
            # 2026-09-08：免费档定义（admin「一键免费档」按钮取值；后端 config 唯一维护点）
            "free": {"platform": _settings.free_llm_platform, "model": _settings.free_llm_model}}


class FeedbackUpdate(BaseModel):
    # D13（2026-08-10）：三态枚举（撤销走独立 revoke 接口 /feedback/{fid}/revoke，不经此接口）
    status: Literal["new", "processing", "done"] | None = None
    reply: str | None = None


@router.get("/feedback")
async def feedback_list(status: str | None = None):
    """反馈列表（细化到提交用户：LEFT JOIN users 取用户名）。"""
    engine = get_global_engine()
    q = select(
        Feedback.id, Feedback.department_id, Feedback.content, Feedback.contact, Feedback.status,
        Feedback.user_id, User.username, Feedback.reply, Feedback.operator,
        Feedback.feedback_type, Feedback.page, Feedback.screenshots,
        Feedback.processed_at, Feedback.created_at,
    ).outerjoin(User, User.id == Feedback.user_id).order_by(Feedback.created_at.desc()).limit(100)
    if status:
        q = q.where(Feedback.status == status)
    async with engine.connect() as conn:
        rows = (await conn.execute(q)).all()
    return {
        "items": [
            {"id": r.id, "department_id": r.department_id, "content": r.content,
             "contact": r.contact, "status": r.status,
             "user_id": r.user_id, "username": r.username or "-", "reply": r.reply, "operator": r.operator,
             "feedback_type": r.feedback_type, "page": r.page, "screenshots": r.screenshots,
             "processed_at": r.processed_at.isoformat() if r.processed_at else None,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows
        ]
    }


@router.put("/feedback/{fid}")
async def feedback_update(fid: int, req: FeedbackUpdate, user: dict = Depends(require_admin)):
    """处理反馈：状态流转（new→processing→done，D13 服务端校验非法跳转）+ 回复 + 处理人/时间。

    D13（2026-08-10）：合法流转仅 new→processing/done、processing→done；done 不可回退；
    置 done 必须已有 reply 或本次带 reply（处理结果可追溯）。撤销走 /feedback/{fid}/revoke。
    """
    values: dict = {}
    if req.status:
        values["status"] = req.status
    if req.reply is not None:
        values["reply"] = req.reply
    if not values:
        raise HTTPException(400, detail={"code": "E011", "message": "无更新内容"})
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(Feedback.status, Feedback.reply).where(Feedback.id == fid)
            )
        ).first()
        if row is None:
            raise HTTPException(404, detail={"code": "E011", "message": "反馈不存在"})
        cur_status = row.status or "new"
        if req.status and req.status != cur_status:
            valid = {
                "new": {"processing", "done"},
                "processing": {"done"},
                # done/revoked 为终态，不允许回退
                "done": set(),
                "revoked": set(),
            }
            if req.status not in valid.get(cur_status, set()):
                raise HTTPException(400, detail={"code": "E011",
                                                 "message": f"非法的状态流转：{cur_status} → {req.status}"})
        if req.status == "done":
            final_reply = req.reply if req.reply is not None else (row.reply or "")
            if not final_reply.strip():
                raise HTTPException(400, detail={"code": "E011",
                                                 "message": "置为已处理前请先填写处理回复"})
            values["processed_at"] = func.now()
            values["operator"] = user["username"]
        await conn.execute(update(Feedback).where(Feedback.id == fid).values(**values))
    await _audit(user["username"], "feedback.update", str(fid), {"status": req.status, "reply": (req.reply or "")[:50]})
    return {"ok": True}


# ===== 数据备份（三期：平台全量导出 / 一键导入覆盖）=====

@router.get("/data/export")
async def admin_data_export(request: Request, user: dict = Depends(require_admin)):
    """导出平台全量数据（全局库全部表 + 各团队库）为 JSON 文件。"""
    from fastapi.responses import Response

    from app.services.data_backup import export_all

    data = await export_all()
    content = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    fname = f"platform_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    # F16（红队二次）：全量导出（含密码哈希）无审计留痕——攻击者导出后管理员无感知；
    # 与 data.import / session.terminate 等敏感操作对齐，导出即落 audit_log
    # R10（红队三修复）：补客户端 IP（原 ip 恒空，攻击溯源受损）
    await _audit(user["username"], "data.export", fname,
                 {"bytes": len(content), "tables": len(data) if isinstance(data, dict) else None},
                 ip=request.client.host)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/data/import")
async def admin_data_import(request: Request, file: UploadFile = File(...),
                            user: dict = Depends(require_admin)):
    """一键导入覆盖：校验 → 备份当前 → 覆盖全局库与团队库。危险操作，前端须二次确认。"""
    from app.services.data_backup import import_all

    content = await file.read()
    if len(content) > 100 * 1024 * 1024:  # N7：导入备份 JSON 上限 100MB（防整读打爆内存）
        raise app_error("E003", "备份文件超过 100MB 限制", status_code=400)
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, detail={"code": "E011", "message": f"文件不是有效 JSON: {str(e)[:100]}"})
    try:
        stats = await import_all(payload)
    except ValueError as e:
        raise HTTPException(400, detail={"code": "E011", "message": sanitize_err_text(str(e))})  # R6
    except Exception as e:
        logger.exception("数据导入失败")
        raise HTTPException(500, detail={"code": "E011", "message": f"导入失败: {sanitize_err_text(str(e))}"})  # R6
    await _audit(user["username"], "data.import", file.filename or "backup.json", {"stats": stats},
                 ip=request.client.host)  # R10：补客户端 IP
    return {"ok": True, "stats": stats}


# 2026-09-17：原 POST /admin/sync/ceo（手动触发 CEO 库同步；2026-08-24 起已停用）随数据查询线下线删除
# （函数 run_ceo_sync 与 ceo_sync.py 同批删除，前端无调用方）


# ===== 日志与监控 =====

@router.get("/logs/errors")
async def log_errors(limit: int = 100):
    """读取应用日志尾部（错误/警告行）。"""
    log_file = Path(f"{_settings.log_dir}/app.log")
    if not log_file.exists():
        return {"errors": []}
    lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    errors = [l for l in lines if "ERROR" in l or "WARNING" in l][-limit:]
    return {"errors": errors}


@router.get("/logs/login-failures")
async def login_failures():
    """近 24h 登录失败（读 Redis 失败计数。一期返回提示）。"""
    return {"items": [], "note": "登录失败记录见 Redis auth:fail:*（一期不做聚合报表）"}


@router.get("/ip-blacklist")
async def ip_blacklist():
    return {"ips": [], "note": "IP 黑名单存 Redis blk_ip:*"}


@router.delete("/ip-blacklist/{ip}")
async def ip_blacklist_delete(ip: str, user: dict = Depends(require_admin)):
    """解封该 IP：清封禁键 + 失败计数。

    2026-09-10 同类排查（键作用域不一致）：封禁键是**双因子** `auth:block:{ip}:{username}`
    （security._ip_key，防全站 IP 连坐），而解封只删 `auth:block:{ip}` → 键对不上，
    **运维点了"解封"完全无效**，用户被 24h TTL 一直挡着。同时清 `auth:fail:*` 计数——
    不清的话失败计数仍在阈值之上，用户下一次输错就立刻再被封。
    """
    blocked = await redis_scan_delete(f"auth:block:{ip}:*")
    fails = await redis_scan_delete(f"auth:fail:{ip}:*")
    await redis_delete(f"auth:block:{ip}", f"auth:fail:{ip}")
    await _audit(user["username"], "ip.unblock", ip, {"blocked_keys": blocked, "fail_keys": fails})
    return {"ok": True, "unblocked": blocked, "failures_cleared": fails}


@router.get("/audit-logs")
async def audit_logs(limit: int = 100, format: str = "json"):
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500))))).all()  # M6：limit clamp
    items = [
        {
            "id": r.id, "operator": r.operator, "action": r.action, "target": r.target,
            "detail": r.detail, "ip": r.ip,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    if format == "csv":
        import csv
        import io

        buf = io.StringIO()
        buf.write("﻿")  # 2026-08-21（编码隐患 M1）：UTF-8 BOM——Excel 打开中文不乱码
        writer = csv.DictWriter(buf, fieldnames=["id", "operator", "action", "target", "detail", "ip", "created_at"])
        writer.writeheader()
        for it in items:
            writer.writerow({k: str(v) for k, v in it.items()})
        return {"csv": buf.getvalue()}
    return {"items": items}


# ===== 系统维护 =====

@router.get("/sessions")
async def active_sessions():
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(Session).order_by(Session.last_activity_at.desc()).limit(100))).all()
    return {
        "sessions": [
            {"id": r.id, "title": r.title, "client_id": r.client_id, "dept_id": r.department_id,
             "is_readonly": r.is_readonly,
             "last_activity_at": r.last_activity_at.isoformat() if r.last_activity_at else None}
            for r in rows
        ]
    }


@router.post("/sessions/{session_id}/terminate")
async def session_terminate(request: Request, session_id: str, user: dict = Depends(require_admin)):
    # F5（红队二次）：session_id 无 UUID 校验时 Path 拼接 + rmtree 可路径穿越删任意目录。
    # 现虽被 nginx/Starlette 规范化挡在 HTTP 层（红队链 5），纵深缺口仍补——对齐 chat.py _is_uuid
    if not _is_uuid(session_id):
        raise app_error("E011", "非法的会话 ID", status_code=422)

    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM chat_messages WHERE session_id = :s"), {"s": session_id})
        await conn.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": session_id})
    # M23：物理清理（对齐 delete_session——原只删 DB 行，产出/上传目录孤儿残留，
    # 且用户浏览器中的该会话立即失效——后续上传/发送返回"会话不存在"）
    out_dir = Path(f"{_settings.output_dir}/{session_id}")
    if out_dir.exists():
        await asyncio.to_thread(shutil.rmtree, out_dir, True)
    for sub in Path(_settings.upload_dir).glob(f"users/*/{session_id}"):
        if sub.is_dir():
            await asyncio.to_thread(shutil.rmtree, sub, True)
    await _audit(user["username"], "session.terminate", session_id, ip=request.client.host)  # R10
    return {"ok": True}


@router.post("/users/{user_id}/terminate")
async def user_terminate(user_id: int, user: dict = Depends(require_admin)):
    """注销用户账号（产品需求 2026-08-06）：删除账号与全部业务数据（FK 级联）+ 物理目录清理。

    保护：不能注销自己；admin/ceo 为预置账号不可注销。
    注销后该用户全部 token 立即失效（middleware 查 users 行不存在 → 401）。
    """
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(select(User).where(User.id == user_id))).first()
        if row is None:
            raise app_error("E011", "用户不存在", status_code=404)
        if row.id == user["user_id"]:
            raise app_error("E011", "不能注销当前登录账号", status_code=400)
        if row.role in ("admin", "ceo"):
            raise app_error("E011", "预置账号（运维管理/CEO）不可注销", status_code=400)
        # 物理清理定位：该用户全部会话与 resume 批次
        sids = [str(r[0]) for r in (await conn.execute(text("SELECT id FROM sessions WHERE user_id=:u"), {"u": user_id})).all()]
        rids = [str(r[0]) for r in (await conn.execute(text("SELECT id FROM resume_batches WHERE user_id=:u"), {"u": user_id})).all()]
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": user_id})  # CASCADE 删业务数据
    # 2026-09-10（缓存核查）：CASCADE 会删掉该用户的个人知识库文档，
    # 团队管理员的 file_search 缓存里仍留有这些内容的片段（≤10 分钟）——注销后必须清
    from app.services.kb_service import clear_kb_match_cache

    await clear_kb_match_cache()
    # 物理清理（M23/N8 对齐：会话产出/上传目录 + 简历批次目录）
    for sid in sids:
        out_dir = Path(f"{_settings.output_dir}/{sid}")
        if out_dir.exists():
            await asyncio.to_thread(shutil.rmtree, out_dir, True)
        for sub in Path(_settings.upload_dir).glob(f"users/*/{sid}"):
            if sub.is_dir():
                await asyncio.to_thread(shutil.rmtree, sub, True)
    for rid in rids:
        d = Path(f"{_settings.tools_data_dir}/resume/{rid}")
        if d.exists():
            await asyncio.to_thread(shutil.rmtree, d, True)
    await _audit(user["username"], "user.terminate", f"{user_id}:{row.username}")
    return {"ok": True}


@router.post("/cache/clear")
async def cache_clear(user: dict = Depends(require_admin)):
    # M6：DEL 不支持通配符——改 SCAN 逐键删除（真实键 auth:fail:{ip}）
    # 2026-09-10（缓存核查）：原只清 auth:fail:*，运维点「清缓存」后排障仍看到旧工具结果 →
    # 一并清检索/导航/取数缓存（sql_cache 由导入链路负责，此处一并清便于人工排障）
    # 2026-09-10 同类排查：原清单漏了 file_parse（TTL 900s）/ web_search（TTL 1h）/ hist_sum——
    # 运维点「清缓存」排障时这些仍然返回旧值，等于没清干净。
    # 2026-09-17（缓存审计 P2）：补当天新增的缓存键——img_rec（图片识别）。新增缓存务必登记到这里，
    # 否则运维排障点「清缓存」时它们还在返回旧值（同样的坑 09-10 踩过一次）。
    # 不含 auth:block:* / auth:verblock:*：那是安全封禁键，解封走 DELETE /admin/ip-blacklist/{ip}，
    # 不能被"清缓存"顺手放开。
    deleted = 0
    for pattern in ("auth:fail:*", "file_search:*",
                    "kb_match:*", "sql_cache:*", "file_parse:*", "web_search:*", "hist_sum:*",
                    "img_rec:*"):
        try:
            deleted += await redis_scan_delete(pattern)
        except Exception:
            pass
    await _audit(user["username"], "cache.clear", f"deleted={deleted}")
    return {"ok": True, "deleted": deleted}


@router.get("/jobs")
async def jobs_status():
    # E-11(API，2026-08-10)：从 APScheduler 实时读取（原硬编码 2 个与实际 8 个注册 job 不符，
    # 运维按端点判断任务状态会误判）
    from app.core.scheduler import list_jobs

    return {"jobs": list_jobs()}


# ===== v2（D7，2026-08-14）：管理账号查看用户详细会话记录 =====
@router.get("/users/{user_id}/sessions")
async def user_sessions(user_id: int, user: dict = Depends(require_admin)):
    """用户会话列表（细到个人；只读追踪用）。"""
    async with get_global_engine().connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT s.id, s.title, s.mode, s.last_activity_at, "
                     "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS msg_count "
                     "FROM sessions s WHERE s.user_id = :uid ORDER BY s.last_activity_at DESC LIMIT 100"),
                {"uid": user_id},
            )
        ).all()
    await _audit(user["username"], "user.sessions.view", f"{user_id}")
    return {
        "sessions": [
            {
                "id": r[0], "title": r[1], "mode": r[2] or "quick",
                "last_activity_at": r[3].isoformat() if r[3] else None,
                "msg_count": r[4],
            }
            for r in rows
        ]
    }


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, user: dict = Depends(require_admin)):
    """指定会话完整消息流（只看发消息与回复，不含文件内容）。"""
    async with get_global_engine().connect() as conn:
        sess = (await conn.execute(text("SELECT id, user_id FROM sessions WHERE id = :s"), {"s": session_id})).first()
        if sess is None:
            raise app_error("E011", "会话不存在", status_code=404)
        rows = (
            await conn.execute(
                text("SELECT role, content, round_id, created_at FROM chat_messages "
                     "WHERE session_id = :s ORDER BY created_at, round_id"),
                {"s": session_id},
            )
        ).all()
    await _audit(user["username"], "session.messages.view", f"{session_id}:{sess[1]}")
    return {
        "session": {"id": sess[0], "user_id": sess[1]},
        "messages": [
            {
                "role": r[0],
                "content": (r[1] or "")[:20000],  # 超长内容截断（追踪只看脉络）
                "round_id": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ],
    }

