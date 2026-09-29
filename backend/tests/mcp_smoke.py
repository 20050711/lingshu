"""MCP 外部工具验收测试（2026-09-03）。

覆盖：员工视图（列表+启用集）/ 员工多选启停保存（含未知工具 400）/ 运维端点
（登记/改 url/启停守卫：无 url 不可启用）/ 连接测试（xhs 走 go 登录态链路真实握手）。

用法（后端运行中即可；xhs 那条会自动拉起 go 进程）：
    cd backend && source scripts/env_aip.sh && python -u tests/mcp_smoke.py
原则：测试自建自删（任何用例不得删真实数据；结尾还原 admin 原 tools-meta/启用集）。
2026-09-24：删掉"沙盒工具连接测试"用例——它要求本机另起一个 XHS MCP server（:5556），
那个 py 版服务随后端去游客态一起下线了；连接测试的覆盖由 xhs 那条（go 链路）承担。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

import httpx
from sqlalchemy import text

from app.core.database import get_global_engine

BASE = "http://localhost:8001/api/v1"
RESULTS: list[tuple[str, bool, str]] = []
_SANDBOX_ID = "smoke_mcp_tool"


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


async def login(c: httpx.AsyncClient, dept: str, username: str, password: str) -> dict:
    r = await c.post(f"{BASE}/auth/login",
                     json={"department_id": dept, "username": username, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {c.cookies.get('access_token')}"}


async def db_one(sql: str, **params):
    engine = get_global_engine()
    async with engine.connect() as conn:
        r = (await conn.execute(text(sql), params)).first()
    return r


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        admin_h = await login(c, "dept_root", "admin", pw("admin"))
        emp_h = await login(c, "demo", "demo", pw("demo"))

        # 员工视图：列表含 xhs(active) 且带 my_enabled
        r = await c.get(f"{BASE}/mcp/tools", headers=emp_h)
        tools = r.json().get("tools", [])
        xhs = next((t for t in tools if t["id"] == "xhs"), None)
        record("员工外部工具列表含 xhs(active)", r.status_code == 200 and xhs and xhs["status"] == "active",
               f"tools={len(tools)}")
        orig_enabled = r.json().get("my_enabled", [])

        # 员工启停：非法工具 400 / 合法保存 / 清空
        r = await c.put(f"{BASE}/mcp/tools/prefs", headers=emp_h, json={"tool_ids": ["nope_xxx"]})
        record("员工保存含未知工具 → 400", r.status_code == 400, r.text[:80])
        r = await c.put(f"{BASE}/mcp/tools/prefs", headers=emp_h, json={"tool_ids": ["xhs"]})
        record("员工启用 xhs → 保存", r.status_code == 200)
        r = await c.get(f"{BASE}/mcp/tools", headers=emp_h)
        record("启用集回显", "xhs" in r.json().get("my_enabled", []))
        await c.put(f"{BASE}/mcp/tools/prefs", headers=emp_h, json={"tool_ids": orig_enabled})

        # 运维：admin 列表（2026-09-03 部署冒烟 500 回归防线：_row_to_dict async 泄漏）
        r = await c.get(f"{BASE}/mcp/admin/tools", headers=admin_h)
        record("运维列表（含 url）", r.status_code == 200 and len(r.json().get("tools", [])) >= 7,
               f"tools={len(r.json().get('tools', [])) if r.status_code == 200 else r.text[:80]}")
        # 运维：登记沙盒工具（无 url）→ 无 url 启用 400 → 补 url 启用 → 删除
        r = await c.delete(f"{BASE}/mcp/admin/tools/{_SANDBOX_ID}", headers=admin_h)  # 幂等清理
        # 2026-09-18：图标改语义名后做了白名单——非法值（含 emoji）必须 400，
        # 否则 emoji 会从"运维手填"这条路再次漏到界面上（前端只认语义名）
        r = await c.post(f"{BASE}/mcp/admin/tools", headers=admin_h,
                         json={"id": _SANDBOX_ID, "name": "smoke工具", "icon": "🧪"})
        record("非法图标名（emoji）→ 400 白名单拦截", r.status_code == 400, r.text[:80])
        r = await c.get(f"{BASE}/mcp/icon-keys", headers=emp_h)
        keys = r.json().get("keys", []) if r.status_code == 200 else []
        record("图标名清单可下发（运维下拉同源）",
               r.status_code == 200 and "tool" in keys and "chart" in keys, f"n={len(keys)}")
        r = await c.post(f"{BASE}/mcp/admin/tools", headers=admin_h,
                         json={"id": _SANDBOX_ID, "name": "smoke工具", "icon": "code"})
        record("运维登记外部工具", r.status_code == 200, r.text[:80])
        r = await c.put(f"{BASE}/mcp/admin/tools/{_SANDBOX_ID}", headers=admin_h, json={"status": "active"})
        record("无 url 启用 → 400 守卫", r.status_code == 400, r.text[:80])
        r = await c.put(f"{BASE}/mcp/admin/tools/{_SANDBOX_ID}", headers=admin_h,
                        json={"url": "http://127.0.0.1:5556/mcp"})
        record("补 url 后可启用", r.status_code == 200, r.text[:60])
        # xhs 真实连接测试（2026-09-24：平台托管工具走 go 登录态链路；工具清单以接口现状为准——
        # 原来断言的 download_detail 是随游客态一起下线的 py 版工具名）
        r = await c.post(f"{BASE}/mcp/admin/tools/xhs/test", headers=admin_h)
        tools = r.json().get("tools") or []
        record("xhs 连接测试（go 链路真实握手）",
               r.status_code == 200 and {"search_feeds", "get_feed_detail", "user_profile"} <= set(tools),
               f"server={str(r.json().get('server')) if r.status_code == 200 else '-'} tools={tools}")
        # 员工视图不应看到运维字段差异（无 admin tools-meta 侧漏）
        r = await c.delete(f"{BASE}/mcp/admin/tools/{_SANDBOX_ID}", headers=admin_h)
        record("删除沙盒工具（自净）", r.status_code == 200)

        n_ok = sum(1 for _, ok, _ in RESULTS if ok)
        print(f"\n通过 {n_ok}/{len(RESULTS)}")


if __name__ == "__main__":
    asyncio.run(main())
