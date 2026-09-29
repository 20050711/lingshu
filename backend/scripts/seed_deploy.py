"""一期测试部署专用种子：全新空库建表 + 平台/部署团队 + 3 账号（幂等）。

- 团队：deploy/部署验证（create_dept 自动建 dept_deploy_db）；平台团队 ceo/dept_root 直接插行
  （create_dept 对平台团队会 raise——不创建业务库，ceo 实际用 tardis_ceo_db，dept_root 无业务库）
- 账号：deploy01（部署验证/employee）、admin（运维管理/admin）、ceo（CEO/ceo），强密码
- model_layer.default 四段结构照抄 seed.py（缺则插；全新库不存在旧结构，无需 seed.py 的升级分支）
- 不写知识库示例（部署机空库起步，无 market 团队）
- 幂等：建表/团队/账号/model_layer/MCP 均可重复执行

用法（部署机，代码由开发机 rsync 推送）:
    cd backend && conda run -n aip python scripts/seed_deploy.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import insert, select, text

from app.core.database import get_global_engine
from app.core.security import hash_password
from app.models import Base, SystemConfig, User
from app.services.dept_service import create_dept

DEPT_ID = "deploy"
DEPT_NAME = "部署验证"

# username, password, dept_id, role（强密码写法参照 seed_press_dept.py）
# 2026-09-01：初始密码支持 env 注入（DEPLOY01_PASSWORD/ADMIN_PASSWORD/CEO_PASSWORD），
# 未注入时回退下述默认值并打警告——部署机初始凭据不应与开发机同密码长期共存
import os as _os
import sys as _sys

_PW_DEFAULTS = {
    "deploy01": "Deploy#2026$Vrf1",
    "ceo": "Ceo#2026$Exec9",
}
# 2026-09-10（用户要求：凭证别随同步带出去）：**admin 不再内置默认值**——原默认值就是
# 开发机 admin 的密码，scripts/ 随发布同步到部署机等于把平台管理员口令一起发出去。
_ENV_KEYS = {"deploy01": "DEPLOY01_PASSWORD", "admin": "ADMIN_PASSWORD", "ceo": "CEO_PASSWORD"}


def _pw(key: str) -> str:
    """env 未注入时回退默认值并警告（部署机初始凭据建议 env 注入，避免与开发机同密码）。"""
    if key not in _PW_DEFAULTS:
        raise SystemExit(f"[错误] 必须注入 {_ENV_KEYS[key]}：{key} 无内置默认值"
                         f"（2026-09-10 起——原默认值是平台管理员口令，不入库不随同步外发）")
    print(f"[警告] {_ENV_KEYS[key]} 未设置，{key} 使用默认初始密码（建议部署时注入环境变量）", file=_sys.stderr)
    return _PW_DEFAULTS[key]


USERS = [
    ("deploy01", _os.environ.get(_ENV_KEYS["deploy01"]) or _pw("deploy01"), "deploy", "employee"),
    ("admin", _os.environ.get(_ENV_KEYS["admin"]) or _pw("admin"), "dept_root", "admin"),
    ("ceo", _os.environ.get(_ENV_KEYS["ceo"]) or _pw("ceo"), "ceo", "ceo"),
]

# 平台团队：create_dept 会 raise（不建业务库），直接插行
PLATFORM_DEPTS = [("ceo", "CEO"), ("dept_root", "运维管理")]

DEFAULT_LAYER = (
    '{"employee": {"llm": {"platform": "deepseek", "model": "deepseek-v4-flash", "effort": "high", "thinking": true}, '
    '"llm_aux": {"platform": "deepseek", "model": "deepseek-v4-flash", "effort": "high", '
    '"usage": ["kb_rank", "kb_summary", "memory_extract", "video", "resume"]}, '
    '"vision": {"platform": "glm", "model": "glm-4.1v-thinking-flash"}, '
    '"image": {"platform": "glm", "model": "cogview-3-flash"}}, '
    '"ceo": {"llm": {"platform": "deepseek", "model": "deepseek-v4-pro", "effort": "max", "thinking": true}, '
    '"llm_aux": {"platform": "deepseek", "model": "deepseek-v4-pro", "effort": "high", '
    '"usage": ["kb_rank", "kb_summary", "memory_extract", "video", "resume"]}, '
    '"vision": {"platform": "glm", "model": "glm-4.1v-thinking-flash"}, '
    '"image": {"platform": "glm", "model": "cogview-3-flash"}}}'
)


async def main() -> int:
    engine = get_global_engine()
    async with engine.begin() as conn:
        # 全新库建表（seed.py 同款；部署机没有先跑过 seed.py 的依赖）
        await conn.run_sync(Base.metadata.create_all)

        # 平台团队（幂等）
        for dept_id, name in PLATFORM_DEPTS:
            await conn.execute(
                text("INSERT INTO departments (dept_id, name) VALUES (:d, :n) ON CONFLICT (dept_id) DO NOTHING"),
                {"d": dept_id, "n": name},
            )
        print(f"platform departments ensured: {[d for d, _ in PLATFORM_DEPTS]}")

    # 部署团队（create_dept 幂等：已存在跳过 + 补建 dept_deploy_db）
    try:
        await create_dept(DEPT_ID, DEPT_NAME)
        print(f"department ensured: {DEPT_ID}/{DEPT_NAME}")
    except ValueError as e:
        print(f"[错误] 团队创建失败: {e}")
        return 1

    # 账号（幂等，按 (dept_id, username)）+ model_layer.default（缺则插）
    created = 0
    async with engine.begin() as conn:
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
                created += 1
                print(f"user created: {username} ({dept_id}/{role})")
            else:
                print(f"user exists: {username} ({dept_id}/{role})")

        existing_cfg = {r[0] for r in (await conn.execute(select(SystemConfig.key))).all()}
        if "model_layer.default" not in existing_cfg:
            await conn.execute(
                insert(SystemConfig).values(
                    key="model_layer.default",
                    value=DEFAULT_LAYER,
                    description="默认模型分层（B4：JSON 四段 llm/llm_aux/vision/image，可按团队覆盖 model_layer.{dept_id}）",
                )
            )
            print("system_config: model_layer.default seeded")
        else:
            print("system_config: model_layer.default exists")

    # MCP 静态数据（幂等）
    from app.api.mcp import ensure_mcp_seed

    await ensure_mcp_seed()
    print("mcp tools: ensured")

    await engine.dispose()
    print(f"=== seed_deploy done: {created} users created ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
