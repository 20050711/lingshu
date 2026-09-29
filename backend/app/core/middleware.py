"""认证依赖与请求上下文。

- get_current_user: 解析 JWT → 返回用户信息（含 dept_id/role）
- require_admin: 仅 admin 角色可访问
- 请求上下文携带 client_id（X-Client-ID 头，前端登录时生成的 UUID）

L11/L21（2026-08-06）：token 来源 cookie 优先（httpOnly，前端 JS 不可读防 XSS），
Authorization header 兼容（API 客户端/测试脚本）；每请求校验 users.token_version
（登出/注销按用户吊销，旧 token 立即失效）与 status（禁用即时生效）。
"""
from __future__ import annotations

from fastapi import Depends, Header, Request
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.logging import get_logger
from app.core.redis import redis_delete, redis_get, redis_incr, redis_set
from app.core.security import decode_token, get_client_ip
from app.models import User

_settings = get_settings()
logger = get_logger("core.middleware")

USER_CTX_KEY = "current_user"


def get_client_id(x_client_id: str | None = Header(default=None)) -> str | None:
    return x_client_id


def _unauthorized(reason: str = "") -> Exception:
    # 2026-09-02：偶发 401 排查——各失败分支带原因落 [AUTHDBG]（uvicorn access log 无认证失败细节；
    # 历史同前缀先例见旧 logout 日志），下次偶发可直接定位分支
    if reason:
        logger.info("AUTHDBG 401 reason=%s", reason)
    return app_error("E006", "未授权访问", status_code=401)


async def _extract_token(request: Request, authorization: str | None) -> str | None:
    """L11：Authorization header 优先（API 客户端/测试脚本——其 cookie jar 可能残留其他账号
    登录态），cookie 兜底（前端主通道——前端已不传 header，经 /api 代理自动携带 cookie）。"""
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return request.cookies.get("access_token")


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    token = await _extract_token(request, authorization)
    if not token:
        raise _unauthorized("no-token")
    payload = await decode_token(token)
    if payload is None:
        raise _unauthorized("decode-fail")
    # R2（红队三修复）：ver 伪造试探封禁——被封 IP 直接拒绝（decode 后、DB 前；
    # 正常用户无试探失败不触发；开发环境单 IP 攻击测试会封本机，测试后清 Redis 键；
    # 生产/压测可经 VER_BLOCK_ENABLED=false 关闭）
    ip = get_client_ip(request)
    if _settings.ver_block_enabled and await redis_get(f"auth:verblock:{ip}"):
        raise _unauthorized("ip-blocked")
    # L21：吊销校验——对比 users.token_version（登出/注销 +1 后旧 token 全失效）+ status（禁用即时生效）
    async with get_global_engine().connect() as conn:
        row = (
            await conn.execute(select(User).where(User.id == int(payload["sub"])))
        ).first()
    if row is None:
        raise _unauthorized("user-gone")  # 用户已注销/不存在
    if row.status != "active":
        raise _unauthorized("user-disabled")
    if payload.get("ver", 0) != row.token_version:
        # R2（红队三修复）：伪造 token 的 ver 试探失败——按 IP 计数封禁（纵深；
        # 正常用户 token 匹配不触发；XFF 伪造在 TRUSTED_PROXIES 未配时不可信）
        if _settings.ver_block_enabled and not await redis_get(f"auth:verblock:{ip}"):
            fails = await redis_incr(f"auth:verfail:{ip}", 900)
            if fails >= 20:
                await redis_set(f"auth:verblock:{ip}", "1", 3600)
        raise _unauthorized(f"ver-mismatch ver={payload.get('ver', 0)} db={row.token_version}")
    # R2 误伤抑制：ver 校验成功即清失败计数——登出/吊销后旧 cookie 偶尔访问的
    # 正常用户（失败 1-2 次即停）不会累积触发封禁；只有持续失败的伪造试探会封
    if await redis_get(f"auth:verfail:{ip}"):
        await redis_delete(f"auth:verfail:{ip}")
    user = {
        "user_id": row.id,
        "username": row.username,
        "role": row.role,
        "dept_id": row.department_id,
        "jti": payload["jti"],
    }
    request.state.current_user = user
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise app_error("E006", "仅管理员可访问", status_code=403)
    return user


def require_role(*roles: str):
    """角色守卫工厂（四期重构：支持多角色，如 require_role('ceo') 或 require_role('admin','dept_admin')）。"""
    async def _check(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise app_error("E006", f"仅 {roles} 角色可访问", status_code=403)
        return user

    return _check


async def require_admin_or_dept_admin(user: dict = Depends(get_current_user)) -> dict:
    """运维管理或团队管理员（团队级权限由具体接口按 dept_id 二次校验）。

    4.1：CEO 兼任自己团队（ceo 团队）管理员——可管理本团队知识库与技能。
    """
    if user["role"] not in ("admin", "dept_admin", "ceo"):
        raise app_error("E006", "仅管理员可访问", status_code=403)
    return user


async def get_business_user(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """业务接口守卫（四期重构）：admin 不参与业务问答；但可按团队视角放行
    （运维以「团队视角」查看/管理业务数据时需携带 X-Dept-Id）。"""
    if user["role"] == "admin":
        dept = request.headers.get("x-dept-id")
        if not dept:
            raise app_error("E006", "运维账号请先选择要查看的团队（页面顶部切换）", status_code=403)
        async with get_global_engine().connect() as conn:
            row = (await conn.execute(text("SELECT dept_id FROM departments WHERE dept_id=:d"), {"d": dept})).first()
        if not row:
            raise app_error("E006", "所选团队不存在", status_code=403)
        # 2026-08-31（双端防御）：dept_root（运维管理）无业务数据——团队视角只对业务团队有意义
        if dept == "dept_root":
            raise app_error("E006", "运维管理团队无业务数据", status_code=403)
        return {**user, "dept_id": dept}
    return user
