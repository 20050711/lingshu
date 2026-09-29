"""种子数据脚本：建表 + 预置账号 + MCP/技能静态数据。

用法（conda 环境）：
    cd backend && conda run -n aip python -m scripts.seed
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import insert, select, text

import os

from app.core.database import get_global_engine
from app.core.security import hash_password
from app.models import Base, Department, McpTool, SystemConfig, User

# 2026-09-28（tardis_ai 副本）：最小种子——1 管理员 + 1 普通用户 + 1 演示团队，无业务数据
# 口令从环境变量读；未设置时用占位值，**首次登录后请立即改密**。
#   SEED_ADMIN_PASSWORD / SEED_USER_PASSWORD
USERS = [
    # username, password, department_id, role
    ("admin", os.environ.get("SEED_ADMIN_PASSWORD", "change-me-admin"), "dept_root", "admin"),
    ("user1", os.environ.get("SEED_USER_PASSWORD", "change-me-user"), "demo", "employee"),
]

# 预置团队（dept_root=管理员所属平台团队，无业务库；demo=演示团队，建 tardis_dept_demo_db）
DEPARTMENTS = [
    ("dept_root", "系统管理"),
    ("demo", "演示空间"),
]


async def seed() -> None:
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 用户（幂等；四期重构：账号团队内唯一，按 (dept_id, username) 判断）
        existing = {
            (r[1], r[0])
            for r in (await conn.execute(select(User.username, User.department_id))).all()
        }
        for username, password, dept_id, role in USERS:
            if (dept_id, username) not in existing:
                await conn.execute(
                    insert(User).values(
                        username=username,
                        password_hash=hash_password(password),
                        department_id=dept_id,
                        role=role,
                    )
                )
                print(f"user created: {username}/{password} ({dept_id}/{role})")

        # 团队（幂等）
        existing_depts = {r[0] for r in (await conn.execute(select(Department.dept_id))).all()}
        for dept_id, name in DEPARTMENTS:
            if dept_id not in existing_depts:
                await conn.execute(
                    insert(Department).values(dept_id=dept_id, name=name)
                )
                print(f"department created: {dept_id}/{name}")
        print(f"departments: {len(DEPARTMENTS)} total")

        # 系统配置说明行（三期 M13：model_layer 默认分层，与 env 一致、管理后台可见可改；B4 四段结构）
        existing_cfg = {r[0] for r in (await conn.execute(select(SystemConfig.key))).all()}
        default_layer = (
            '{"employee": {"llm": {"platform": "deepseek", "model": "deepseek-flash", "effort": "high", "thinking": false}, "llm_aux": {"platform": "agnes", "model": "agnes-3.0-flash", "effort": "low", "usage": ["kb_rank", "kb_summary", "memory_extract", "history_summary"], "thinking": false}, "llm_tools": {"platform": "agnes", "model": "agnes-3.0-flash", "effort": "low", "usage": ["meeting", "resume", "video"], "thinking": true}, "vision": {"platform": "agnes", "model": "agnes-3.0-flash"}, "image": {"platform": "glm", "model": "cogview-3-flash"}, "video": {"platform": "agnes", "model": "agnes-video-v2.0"}}, "ceo": {"llm": {"platform": "agnes", "model": "agnes-3.0-flash", "effort": "high", "thinking": false}, "llm_aux": {"platform": "agnes", "model": "agnes-3.0-flash", "effort": "low", "thinking": false}}, "dept_admin": {"llm": {"platform": "deepseek", "model": "deepseek-flash", "effort": "high", "thinking": false}, "llm_aux": {"platform": "agnes", "model": "agnes-3.0-flash", "effort": "low", "usage": ["kb_rank", "kb_summary", "memory_extract", "history_summary"], "thinking": false}, "llm_tools": {"platform": "agnes", "model": "agnes-3.0-flash", "effort": "low", "usage": ["meeting", "resume", "video"], "thinking": true}, "vision": {"platform": "agnes", "model": "agnes-3.0-flash"}, "image": {"platform": "glm", "model": "cogview-3-flash"}, "video": {"platform": "agnes", "model": "agnes-video-v2.0"}}}'
        )
        if "model_layer.default" not in existing_cfg:
            await conn.execute(
                insert(SystemConfig).values(
                    key="model_layer.default",
                    value=default_layer,
                    description="默认模型分层（B4：JSON 四段 llm/llm_aux/vision/image，可按团队覆盖 model_layer.{dept_id}）",
                )
            )
            print("system_config: model_layer.default seeded")
        else:
            # B4：旧结构（employee 段无 llm_aux 键：可能是 {"model","effort"} 或三段结构；或 usage 为旧自由文本）
            # → 升级为四段结构（幂等）
            row = (await conn.execute(
                select(SystemConfig.value).where(SystemConfig.key == "model_layer.default")
            )).first()
            try:
                data = json.loads(row[0]) if row else {}
                seg = data.get("employee")
                aux_usage = (seg.get("llm_aux") or {}).get("usage") if isinstance(seg, dict) else None
                need_upgrade = (
                    not isinstance(seg, dict)
                    or "model" in seg
                    or "llm_aux" not in seg
                    or not isinstance(aux_usage, list)
                )
                if need_upgrade:
                    await conn.execute(
                        text("UPDATE system_config SET value=:v WHERE key='model_layer.default'"),
                        {"v": default_layer},
                    )
                    print("system_config: model_layer.default upgraded to 四段结构（B4）")
            except (json.JSONDecodeError, TypeError):
                pass

        # 四期重构：技能层 yaml 已删除，工具直接注册（skill_configs 为死表不再写入）

        # MCP 工具（幂等）
        from app.api.mcp import MCP_DEFAULTS

        existing_mcp = {r[0] for r in (await conn.execute(select(McpTool.id))).all()}
        for mid, name, icon, desc, order in MCP_DEFAULTS:
            if mid not in existing_mcp:
                await conn.execute(
                    insert(McpTool).values(id=mid, name=name, icon=icon, description=desc, status="planning", sort_order=order)
                )
        print(f"mcp tools: {len(MCP_DEFAULTS)} total")

        # 2026-09-28（tardis_ai 副本）：原"知识库示例文档"（市场部周报等公司业务文案）已删——
        # 最小种子保持零业务数据，知识库由使用者在后台自行上传。

    # 团队业务库（幂等；demo 等真实团队建库，dept_root 平台团队跳过——create_dept 会 raise）
    from app.services.dept_service import create_dept

    for dept_id, name in DEPARTMENTS:
        if dept_id != "dept_root":
            await create_dept(dept_id, name)
            print(f"dept db ensured: {dept_id}")

    await engine.dispose()
    print("=== seed done ===")


if __name__ == "__main__":
    asyncio.run(seed())
