"""认证 API：验证码 / 登录（防暴力破解）/ 登出 / 当前用户。"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.middleware import get_client_id, get_current_user
from app.core.rate_limit import rate_limit_dep
from app.core.redis import redis_delete, redis_get, redis_set
from app.services.dept_service import get_dept_name
from app.core.security import (
    block_ip,
    create_token,
    generate_captcha,
    get_client_ip,
    get_login_failures,
    hash_password,
    is_ip_blocked,
    record_login_failure,
    reset_login_failures,
    validate_password,
    verify_password,
)
from app.models import User

_settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])

# 团队中文名改查 departments 表（三期 M12 多团队正式化，60s 缓存）
# SEC-07：客户端 IP 提取已迁移 core/security.get_client_ip（仅白名单内可信 XFF，供限流复用）


async def _issue_captcha() -> tuple[str, str]:
    """生成验证码并入库，返回 (captcha_id, base64 image)。S4：错误响应携带验证码。"""
    answer, b64 = generate_captcha()
    # R3（红队三修复）：captcha_id 改服务端随机值——原 sha256(answer+secret_key) 在
    # 密钥公开时可离线穷举反推 answer（红队实测 0 秒击穿）；随机 id 与答案无公开映射
    captcha_id = secrets.token_hex(8)
    await redis_set(f"cap:{captcha_id}", answer, _settings.captcha_ttl_seconds)
    return captcha_id, b64


class LoginRequest(BaseModel):
    department_id: str  # 四期重构：登录三要素（团队+账号+密码），账号团队内唯一
    username: str
    password: str
    captcha_id: str | None = None
    captcha_text: str | None = None


class CaptchaResponse(BaseModel):
    captcha_id: str
    captcha_image: str  # base64 PNG


@router.get("/departments")
async def departments(
    _rl: None = Depends(rate_limit_dep("depts", _settings.rate_limit_depts_per_min, by="ip")),  # F22：防未登录批量枚举
):
    """登录页团队下拉数据源（无需登录）：全量团队，含运维管理(dept_root)/CEO。
    2026-09-01：附 preset_username——该团队预置账号（role=admin/ceo）当前用户名，登录页预填锁定用
    （用户可自助改名，硬编码 'admin'/'ceo' 会锁旧名无法登录，改动态取）。"""
    from app.models import Department

    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(select(Department.dept_id, Department.name))
        ).all()
        out = [{"dept_id": d, "name": n} for d, n in rows]
        # 预置账号名（ceo 团队=role=ceo 账号；dept_root=role=admin 账号；其余团队无预置）
        for item in out:
            if item["dept_id"] in ("ceo", "dept_root"):
                row = (
                    await conn.execute(
                        select(User.username).where(
                            User.department_id == item["dept_id"],
                            User.role.in_(("ceo", "admin")),
                            User.status == "active",
                        ).limit(1)
                    )
                ).first()
                item["preset_username"] = row[0] if row else None
            else:
                item["preset_username"] = None
    return {"departments": out}


@router.get("/captcha", response_model=CaptchaResponse)
async def captcha(
    _rl: None = Depends(rate_limit_dep("captcha", _settings.rate_limit_captcha_per_min, by="ip")),  # SEC-05
):
    answer, b64 = generate_captcha()
    captcha_id = secrets.token_hex(8)  # R3：随机 id（同 _issue_captcha）
    # 用 captcha_id 作 key，value 存答案（一次有效）
    await redis_set(f"cap:{captcha_id}", answer, _settings.captcha_ttl_seconds)
    return CaptchaResponse(captcha_id=captcha_id, captcha_image=b64)


@router.post("/login")
async def login(
    req: LoginRequest,
    request: Request,
    _rl: None = Depends(rate_limit_dep("login", _settings.rate_limit_login_per_min, by="ip")),  # SEC-05
):
    ip = get_client_ip(request)
    # E-01：封禁/计数键 = ip + username 双因子（反代同源 IP 下不再全站共享）
    if await is_ip_blocked(ip, req.username):
        raise app_error("E006", "账号暂时无法登录，请稍后重试", status_code=403)

    engine = get_global_engine()
    async with engine.connect() as conn:
        # 四期重构：账号团队内唯一，登录三要素联合查询（团队+账号）
        row = (
            await conn.execute(
                select(User).where(
                    User.department_id == req.department_id,
                    User.username == req.username,
                )
            )
        ).first()

    if row is None or not verify_password(req.password, row.password_hash):
        fails = await record_login_failure(ip, req.username)
        if fails >= _settings.ip_blacklist_threshold:
            await block_ip(ip, req.username)
            raise app_error("E006", "账号暂时无法登录，请稍后重试", status_code=403)
        if fails >= _settings.captcha_fail_threshold:
            captcha_id, b64 = await _issue_captcha()
            raise app_error(
                "E006",
                "账号或密码错误，请完成验证码后重试",
                status_code=401,
                extra={"captcha_id": captcha_id, "captcha_image": b64},
            )
        raise app_error("E006", "账号或密码错误", status_code=401)

    # S4 修复：验证码强制状态机——同 IP 失败次数越过阈值后，即使密码正确也必须校验验证码
    fails = await get_login_failures(ip, req.username)
    if fails >= _settings.captcha_fail_threshold:
        if not (req.captcha_id and req.captcha_text):
            captcha_id, b64 = await _issue_captcha()
            raise app_error(
                "E006",
                "请完成验证码后重试",
                status_code=401,
                extra={"captcha_id": captcha_id, "captcha_image": b64},
            )
        saved = await redis_get(f"cap:{req.captcha_id}")
        # 2026-08-18 用户要求：验证码一次性——只要提交过（无论对错）立即作废，
        # 防同一 captcha_id 重复提交暴力猜答案（原实现仅答对才删，答错可无限重试同一张）
        if saved is not None:
            await redis_delete(f"cap:{req.captcha_id}")
        if saved is None or saved != req.captcha_text:
            captcha_id, b64 = await _issue_captcha()
            raise app_error(
                "E006",
                "验证码错误",
                status_code=401,
                extra={"captcha_id": captcha_id, "captcha_image": b64},
            )

    if row.status != "active":
        raise app_error("E007", "账号已禁用", status_code=403)

    await reset_login_failures(ip, req.username)
    # L11/L21：token 走 httpOnly cookie（JS 不可读防 XSS）；body 不再返回 access_token；
    # ver 绑定 users.token_version（登出/吊销后旧 token 全失效）
    # #3（2026-08-12 演示需求）：单点登录——登录即 token_version+1，旧会话 token 全部失效
    # （新登录踢掉旧登录，被踢方下一请求 401 跳登录页；登出吊销逻辑不变）
    async with engine.begin() as conn:
        await conn.execute(
            update(User).where(User.id == row.id).values(
                token_version=User.token_version + 1,
                last_login_at=func.now(),
            )
        )
    token, _ = create_token(row.id, row.username, row.role, row.department_id, row.token_version + 1)
    response = JSONResponse(
        content={
            "user": {
                "username": row.username,
                "role": row.role,
                "dept_id": row.department_id,
                "dept_name": await get_dept_name(row.department_id),
            },
        }
    )
    # CSRF：SameSite=Lax 挡跨站 POST（内网部署足够）；公网部署时评估 CSRF token
    response.set_cookie(
        "access_token", token,
        httponly=True, samesite="lax", secure=_settings.cookie_secure,
        max_age=_settings.jwt_expire_days * 86400, path="/",
    )
    return response


@router.post("/logout")
async def logout(user: dict = Depends(get_current_user)):
    # L21 方案 A：按用户吊销——ver+1 使该账号全部 token 失效（一台登出全端掉线，产品已确认）
    async with get_global_engine().begin() as conn:
        await conn.execute(
            update(User).where(User.id == user["user_id"]).values(token_version=User.token_version + 1)
        )
    response = JSONResponse(content={"ok": True})
    response.delete_cookie("access_token", path="/")
    return response


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return {
        "user": {
            "username": user["username"],
            "role": user["role"],
            "dept_id": user["dept_id"],
            "dept_name": await get_dept_name(user["dept_id"]),
        }
    }


class AccountUpdateRequest(BaseModel):
    """账号设置（2026-09-01）：自助改账号名/改密码，可同时修改（一次事务 + 一次吊销）。"""
    new_username: str | None = None
    old_password: str | None = None
    new_password: str | None = None


@router.put("/account")
async def update_account(req: AccountUpdateRequest, user: dict = Depends(get_current_user)):
    """账号设置：改账号名（团队内唯一预检）/ 改密码（旧密码校验 + 8-16 位规则）。

    组合设计原因：改名/改密都触发 token_version+1（吊销旧会话），若拆两个接口
    前端连发两个请求时第二个必然 401——合并为一个接口一次事务完成。
    完成即吊销全部旧会话（含当前），前端提示重新登录。
    """
    from app.api.admin import _audit

    want_name = req.new_username is not None and bool(req.new_username.strip())
    want_pwd = req.old_password is not None or req.new_password is not None
    if not want_name and not want_pwd:
        raise app_error("E011", "没有需要修改的内容", status_code=400)
    if want_pwd and (not req.old_password or not req.new_password):
        raise app_error("E011", "修改密码须同时提供原密码与新密码", status_code=400)
    new_username = req.new_username.strip() if want_name else None
    if want_name:
        if not new_username:
            raise app_error("E011", "账号名不能为空", status_code=400)
        if len(new_username) > 50:
            raise app_error("E011", "账号名最长 50 字", status_code=400)
    if want_pwd:
        err = validate_password(req.new_password)
        if err:
            raise app_error("E011", err, status_code=400)
        if req.new_password == req.old_password:
            raise app_error("E011", "新密码不能与原密码相同", status_code=400)

    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(select(User).where(User.id == user["user_id"]))).first()
        if row is None:
            raise app_error("E007", "账号不存在", status_code=404)
        if want_pwd and not verify_password(req.old_password, row.password_hash):
            raise app_error("E006", "原密码不正确", status_code=403)
        if want_name:
            existing = (
                await conn.execute(
                    select(User.id).where(
                        User.department_id == row.department_id,
                        User.username == new_username,
                        User.id != row.id,
                    )
                )
            ).first()
            if existing:
                raise app_error("E011", "该团队下账号已存在", status_code=400)
        values: dict = {"token_version": User.token_version + 1}
        if want_name:
            values["username"] = new_username
        if want_pwd:
            values["password_hash"] = hash_password(req.new_password)
        await conn.execute(update(User).where(User.id == row.id).values(**values))
    await _audit(
        user["username"], "user.account_update", str(row.id),
        {"new_username": new_username or None, "password_changed": bool(want_pwd)},
    )
    return {"ok": True, "username": new_username or user["username"]}
