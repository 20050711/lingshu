"""定制化工具权限（黑名单 + 团队定制化工具白名单）。

- 定制化工具 = /tools 页面各功能（kb 知识库浏览 / meeting 会议纪要 等）
  - 黑名单：system_config key=custom_block.{dept_id}（JSON 数组=禁用清单；缺省=None 无禁用=全部可用）
  - 消费侧：GET /tools/access 返回本团队 blocked（前端置灰/路由守卫）；入口接口经 require_custom_tool 拦截
- 团队定制化工具 = /dtools 板块（简历初筛）
  - 白名单：system_config key=custom_allow.{dept_id}（JSON 数组=已开通清单；**缺省=None=全部关闭**）
  - 与黑名单并存最严生效：白名单未开通或黑名单命中 → 拒绝
  - 消费侧：GET /tools/dept-access 返回 allowed+blocked；入口接口经 require_dept_custom_tool 拦截
- 历史：custom_tools.{dept_id} 白名单 key 已于 2026-09-01 废弃（换 key，存量作废=默认全开）
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.exceptions import app_error
from app.core.middleware import get_business_user

router = APIRouter(tags=["custom-tools"])
_settings = get_settings()

# 定制化工具注册表（id → 中文名；新增工具在此扩展，前端 ToolsPage 同步加卡片）
CUSTOM_TOOLS: dict[str, str] = {
    # 2026-09-02：video/resume 已迁入「团队定制化工具」板块（/dtools，白名单制）——此处移除
    "kb": "知识库浏览",
    "meeting": "会议纪要",  # 2026-08-25：录音 → 转写 → 场景总结
}

# 团队定制化工具注册表（2026-09-02 新建板块 /dtools：白名单制，默认全团队关闭，admin 逐团队开通）
DEPT_CUSTOM_TOOLS: dict[str, str] = {
    "resume": "简历初筛",  # 2026-09-02 迁入（原 /tools/resume）
}


async def get_custom_tools(dept_id: str) -> list[str] | None:
    """团队定制化工具禁用清单；缺省 None=无禁用（全部可用）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT value FROM system_config WHERE key=:k"), {"k": f"custom_block.{dept_id}"}
            )
        ).first()
    if not row or not row[0]:
        return None
    try:
        tools = json.loads(row[0])
        return tools if isinstance(tools, list) else None
    except (ValueError, TypeError):
        return None


async def is_custom_tool_enabled(dept_id: str, tool_id: str) -> bool:
    """定制化工具是否对本团队开放（黑名单制：缺省=全开；命中禁用清单=关闭）。

    2026-09-10：供 **agent 工具层**消费——原先 custom_block 只在 HTTP 入口（require_custom_tool）生效，
    团队停用「知识库」后，agent 侧仍能通过恒注入工具读到数据（file_parse/file_search 等），
    页面关了而对话里读得到。此函数让"可见根清单/检索范围"与开关保持一致。
    """
    blocked = await get_custom_tools(dept_id)
    return not (blocked and tool_id in blocked)


def require_custom_tool(tool_id: str):
    """依赖工厂：定制化工具入口接口的团队黑名单校验（未配置=放行；被禁=403）。"""

    async def dep(user: dict = Depends(get_business_user)):
        blocked = await get_custom_tools(user["dept_id"])
        if blocked is not None and tool_id in blocked:
            raise app_error("E006", "该工具已被本团队停用，请联系团队管理员开通", status_code=403)
        return user

    return dep


# ---------- 团队定制化工具白名单（2026-09-02） ----------

async def get_dept_custom_tools(dept_id: str) -> list[str] | None:
    """团队定制化工具白名单（已开通清单）；缺省 None=未配置（=全部关闭）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT value FROM system_config WHERE key=:k"), {"k": f"custom_allow.{dept_id}"}
            )
        ).first()
    if not row or not row[0]:
        return None
    try:
        tools = json.loads(row[0])
        return tools if isinstance(tools, list) else None
    except (ValueError, TypeError):
        return None


async def is_dept_tool_allowed(dept_id: str, tool_id: str) -> bool:
    """团队定制化工具可见性：白名单未配置（默认关闭）或黑名单命中 → 拒绝；白名单含 tool_id → 放行。"""
    allowed = await get_dept_custom_tools(dept_id)
    if allowed is None or tool_id not in allowed:
        return False
    blocked = await get_custom_tools(dept_id)
    if blocked is not None and tool_id in blocked:
        return False
    return True


def require_dept_custom_tool(tool_id: str):
    """依赖工厂：团队定制化工具入口接口的白名单校验（默认关闭；未开通/被黑名单禁=403）。"""

    async def dep(user: dict = Depends(get_business_user)):
        if not await is_dept_tool_allowed(user["dept_id"], tool_id):
            raise app_error("E006", "该工具未对本团队开通，请联系团队管理员开通", status_code=403)
        return user

    return dep


@router.get("/tools/dept-access")
async def tools_dept_access(user: dict = Depends(get_business_user)):
    """本团队定制化工具开通状态（板块页渲染/守卫：allowed=已开通清单（空/未配置=全部关闭），blocked=黑名单叠加）。"""
    allowed = await get_dept_custom_tools(user["dept_id"])
    blocked = await get_custom_tools(user["dept_id"])
    return {"allowed": allowed or [], "blocked": blocked}


@router.get("/tools/access")
async def tools_access(user: dict = Depends(get_business_user)):
    """本团队定制化工具禁用集合（前端置灰/路由守卫；null=未配置全部可用）。
    2026-08-25：附 https_port——http 协议下点「会议纪要」卡片自动跳转 https（浏览器录音 secure context）。"""
    blocked = await get_custom_tools(user["dept_id"])
    return {"blocked": blocked, "https_port": _settings.https_port}
