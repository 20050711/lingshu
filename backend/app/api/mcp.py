"""MCP 外部工具 API（2026-09-03 从"规划展示"升级为真实接入）。

权限模型（2026-09-03 运维拍板）：
- 运维（admin）全局管理：登记 url / 启停（planning 规划展示 → active 启用 → disabled 停用）/ 连接测试
- 团队管理员不参与
- 员工：AI外部工具页自行多选启用（user_mcp.{uid} 显式启用制，默认全禁）；
  agent 侧消费见 skill_router（mcp 组工具过滤）

mcp_tools.status 语义：planning（规划展示，无 url）/ active（运维启用）/ disabled（运维停用）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import insert, select, text

from app.agent.tools import DEFAULT_ICON, ICON_KEYS
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.middleware import get_current_user, require_admin
from app.models import McpTool
from app.services.config_service import get_user_mcp_enabled, invalidate_mcp_cache, set_user_mcp_enabled
from app.services.mcp_client import McpClient

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _validate_icon(icon: str | None) -> str | None:
    """图标名必须在 ICON_KEYS 内（空值视为"用默认"）。

    2026-09-18：原为自由文本（max_length=10）——运维可填任意字符，emoji 正是从这条路进来的，
    前端也就只能靠一张对照表兜底。改成语义名表后这里做白名单，堵住入口。
    """
    if icon is None or not icon.strip():
        return None
    if icon not in ICON_KEYS:
        raise app_error("E011", f"未知图标名「{icon}」（可选：{'、'.join(ICON_KEYS)}）", status_code=400)
    return icon


@router.get("/icon-keys")
async def icon_keys():
    """可选图标名清单（运维后台的图标下拉读它——不在前端硬编码，避免两处清单漂移）。"""
    return {"keys": list(ICON_KEYS), "default": DEFAULT_ICON}

# 平台网关托管工具（2026-09-10）：进程由后端托管（自动拉起 + 登录态自愈），不经 url 握手——
# 运维启停用同一开关，但"启用前必须配置 MCP server url"的校验对其豁免，前端也按此显示开关/连接测试
PLATFORM_TOOL_IDS: set[str] = set()

# 2026-09-18：图标字段由 emoji 改为**语义名**（取值见 app/agent/tools/__init__.py 的 ICON_KEYS），
# 由前端 components/Icon.tsx 查表画成 Phosphor 图标。
MCP_DEFAULTS: list[tuple[str, str, str, str, int]] = [
    # 真机浏览器驱动（real-browser/）：以 Streamable HTTP 暴露，子工具名 `browse_*` 归属本条目
    ("browse", "浏览器自动化", "globe",
     "驱动真实浏览器实例完成网页任务：持久化登录态、拟人化操作节奏、内建行为护栏；"
     "不以指纹伪造取胜，而以真实性获得稳定性（长任务成功率优先）。", 1),
]


async def ensure_mcp_seed() -> None:
    """MCP 静态数据幂等写入（seed 时调用）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        existing = {r[0] for r in (await conn.execute(select(McpTool.id))).all()}
        for mid, name, icon, desc, order in MCP_DEFAULTS:
            if mid not in existing:
                # 平台托管工具落地即 active；
                # 其余保持"规划展示"
                st = "active" if mid in PLATFORM_TOOL_IDS else "planning"
                await conn.execute(
                    insert(McpTool).values(id=mid, name=name, icon=icon, description=desc, status=st, sort_order=order)
                )


def _row_to_dict(r: Any) -> dict:
    # 2026-09-03（部署冒烟 500）：纯 dict 转换不需要 async——async + 列表推导不 await → coroutine 泄漏
    return dict(r._mapping)


@router.get("/tools")
async def list_mcp_tools(user: dict = Depends(get_current_user)):
    """外部工具列表（业务用户）：active/disabled 可见；planning 规划展示。
    员工返回个人启用集（user_mcp 显式启用制），供页面勾选回显。

    2026-09-10（走查问题）：my_enabled 与「当前 active 集」取交集——工具下线/合并后
    历史启用集里的旧 id（已下线/合并前的工具）会让页面回传保存时 400
    "含未启用/未知工具"，且页面上没有对应卡片可取消（用户视角=卡片怎么点都开不了）。
    """
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(McpTool).order_by(McpTool.sort_order))).all()
    enabled = await get_user_mcp_enabled(user["user_id"])
    active_ids = {r.id for r in rows if r.status == "active"}
    my_enabled = sorted(set(enabled or []) & active_ids)
    return {
        "tools": [
            {"id": r.id, "name": r.name, "icon": r.icon, "description": r.description,
             "status": r.status, "url": r.url}
            for r in rows
        ],
        "my_enabled": my_enabled,
    }


# ---------- 员工自行启停（多选保存） ----------

class McpPrefsIn(BaseModel):
    tool_ids: list[str] = Field(default_factory=list, max_length=50)


@router.put("/tools/prefs")
async def set_my_mcp_prefs(req: McpPrefsIn, user: dict = Depends(get_current_user)):
    """员工保存个人启用的外部工具集（显式启用制；未勾选=停用）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(
            text("SELECT id, status FROM mcp_tools WHERE status IN ('active','disabled')"))).all()
    valid = {r.id for r in rows if r.status == "active"}
    unknown = [x for x in req.tool_ids if x not in valid]
    if unknown:
        raise app_error("E011", f"含未启用/未知工具: {unknown}", status_code=400)
    await set_user_mcp_enabled(user["user_id"], req.tool_ids)
    return {"ok": True}


# ---------- 运维管理（仅 admin） ----------

class McpUpsertIn(BaseModel):
    id: str = Field(..., min_length=2, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    icon: str | None = Field(None, max_length=10)
    description: str | None = Field(None, max_length=500)
    url: str | None = Field(None, max_length=500)


class McpUpdateIn(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    icon: str | None = Field(None, max_length=10)
    description: str | None = Field(None, max_length=500)
    url: str | None = Field(None, max_length=500)
    status: str | None = None


@router.get("/admin/tools")
async def admin_list(user: dict = Depends(require_admin)):
    """运维：全部外部工具（含 url 与状态；platform=平台网关托管，无 url 也应显示启停开关）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(select(McpTool).order_by(McpTool.sort_order))).all()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["platform"] = d["id"] in PLATFORM_TOOL_IDS
        out.append(d)
    return {"tools": out}


@router.post("/admin/tools")
async def admin_create(req: McpUpsertIn, user: dict = Depends(require_admin)):
    """运维：登记新外部工具（status=disabled 待启用）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        exists = (await conn.execute(text("SELECT 1 FROM mcp_tools WHERE id=:i"), {"i": req.id})).first()
        if exists:
            raise app_error("E011", f"工具已存在: {req.id}", status_code=400)
        await conn.execute(text(
            "INSERT INTO mcp_tools (id, name, icon, description, url, status, sort_order) "
            "VALUES (:i, :n, :ic, :d, :u, 'disabled', 100)"),
            {"i": req.id, "n": req.name, "ic": _validate_icon(req.icon) or DEFAULT_ICON,
             "d": req.description, "u": req.url})
    return {"ok": True}


@router.put("/admin/tools/{tool_id}")
async def admin_update(tool_id: str, req: McpUpdateIn, user: dict = Depends(require_admin)):
    """运维：改 url/名称/描述/启停（active↔disabled；planning 仅可补 url 转 active）。"""
    if req.status not in (None, "active", "disabled", "planning"):
        raise app_error("E011", "status 须为 active/disabled/planning", status_code=400)
    icon = _validate_icon(req.icon)   # 未知图标名直接 400（不再自由文本）
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT * FROM mcp_tools WHERE id=:i"), {"i": tool_id})).first()
        if row is None:
            raise app_error("E004", "工具不存在", status_code=404)
        if (req.status == "active" and not (req.url or row.url)
                and tool_id not in PLATFORM_TOOL_IDS):
            raise app_error("E011", "启用前必须配置 MCP server url", status_code=400)
        await conn.execute(text(
            "UPDATE mcp_tools SET name=COALESCE(:n, name), icon=COALESCE(:ic, icon), "
            "description=COALESCE(:d, description), url=COALESCE(:u, url), status=COALESCE(:s, status) "
            "WHERE id=:i"),
            {"i": tool_id, "n": req.name, "ic": icon, "d": req.description,
             "u": req.url, "s": req.status})
    # 2026-09-10：启停立即生效（agent 侧 mcp_active 缓存不再等 60s TTL）
    invalidate_mcp_cache()
    # 2026-09-28：启停/改地址后重发现子工具（后台执行，不阻塞本次响应）
    import asyncio as _asyncio

    from app.agent.tools.mcp_dynamic import refresh_mcp_tools as _refresh

    _asyncio.create_task(_refresh())
    return {"ok": True}


@router.delete("/admin/tools/{tool_id}")
async def admin_delete(tool_id: str, user: dict = Depends(require_admin)):
    """运维：删除登记（真实工具接入中不建议删，幂等种子可重建）。"""
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM mcp_tools WHERE id=:i"), {"i": tool_id})
    invalidate_mcp_cache()      # 下线立即生效（同 admin_update）
    return {"ok": True}


@router.post("/admin/tools/{tool_id}/test")
async def admin_test(tool_id: str, user: dict = Depends(require_admin)):
    """运维：连接测试（initialize 握手 + tools/list）。

    2026-09-10：平台托管工具（PLATFORM_TOOL_IDS）走各自就绪判据；其余走 url 握手。
    """
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM mcp_tools WHERE id=:i"), {"i": tool_id})).first()
    if row is None:
        raise app_error("E004", "工具不存在", status_code=404)
    if not row.url:
        raise app_error("E011", "该工具未配置 url，无法测试", status_code=400)
    try:
        client = McpClient(row.url, timeout=15)
        try:
            info = await client.ping()
            tools = await client.list_tools()
        finally:
            await client.close()
    except Exception as e:
        raise app_error("E011", f"连接失败: {str(e)[:200]}", status_code=400)
    return {"ok": True, "server": info.get("serverInfo"), "tools": [t["name"] for t in tools]}
