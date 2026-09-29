"""四期重构验收测试（check_refactor）：账号三要素 / dept_admin 权限 / 个人记忆 /
技能工具架构（10 工具 / SKILL.md / run_script 兜底恒注入）。

用法：cd backend && source scripts/env_aip.sh && python -u tests/check_refactor.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

import httpx

BASE = "http://localhost:8001/api/v1"

RESULTS: list[tuple[str, bool]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


async def login(c: httpx.AsyncClient, dept: str, username: str, password: str) -> str:
    r = await c.post(f"{BASE}/auth/login", json={"department_id": dept, "username": username, "password": password})
    r.raise_for_status()
    return r.cookies["access_token"]


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        # 1. 登录三要素
        tok_m = await login(c, "demo", "demo", pw("demo"))
        record("登录三要素（团队+账号+密码）", True)
        # 2. 错团队 401
        r = await c.post(f"{BASE}/auth/login", json={"department_id": "demo1", "username": "demo", "password": pw("demo")})
        record("错团队登录 401", r.status_code == 401, f"got {r.status_code}")
        # 3. admin 业务拦截
        tok_a = await login(c, "dept_root", "admin", pw("admin"))
        ah = {"Authorization": f"Bearer {tok_a}"}
        r = await c.post(f"{BASE}/chat/ask", headers=ah, json={"question": "hi"})
        record("运维管理被拦业务问答 403", r.status_code == 403, f"got {r.status_code}")
        # 4. 团队管理员登录 + 知识库删除权限（选本团队文档删除；全局/他团队应 403）
        tok_da = await login(c, "demo", "demo_admin", pw("demo_admin"))
        dah = {"Authorization": f"Bearer {tok_da}"}
        # 修复（2026-08-06）：曾从 KB 列表挑真实文档删除（own=列表第一个 demo 文档）——
        # 每次运行删一篇 seed 文档，多次运行把知识库删光。改为永远自建测试文档再删。
        r = await c.post(f"{BASE}/knowledge/documents", headers=dah,
                         files={"file": ("refactor_test.md", "# 测试文档".encode())})
        ok_del_own = False
        if r.status_code == 200:
            r = await c.delete(f"{BASE}/knowledge/documents/{r.json()['id']}", headers=dah)
            ok_del_own = r.status_code == 200
        record("团队管理员删本团队文档 200", ok_del_own)
        # 他团队/全局权限测试：admin 上传全局文档（department_id=None）→ dept_admin 删应 403
        r = await c.post(f"{BASE}/admin/knowledge/documents", headers=ah,
                         files={"file": ("refactor_global.md", "# 全局测试文档".encode())},
                         data={"title": "refactor_global_test"})
        ok_deny = True
        if r.status_code == 200:
            gid = r.json().get("id")
            r = await c.delete(f"{BASE}/knowledge/documents/{gid}", headers=dah)
            ok_deny = r.status_code == 403
            record("团队管理员删全局文档 403", ok_deny, f"got {r.status_code}")
            await c.delete(f"{BASE}/admin/knowledge/documents/{gid}", headers=ah)  # 清理测试文档
        # 5. 个人记忆 CRUD（admin 接口）
        demo_uid = 2
        r = await c.post(f"{BASE}/admin/memory/users/{demo_uid}", headers=ah,
                         json={"mem_type": "knowledge", "content": "偏好用柱状图"})
        ok_add = r.status_code == 200
        mem_id = r.json().get("id") if ok_add else None
        r = await c.get(f"{BASE}/admin/memory/users/{demo_uid}", headers=ah)
        ok_list = r.status_code == 200 and len(r.json().get("items", [])) > 0
        ok_del = True
        if mem_id:
            r = await c.delete(f"{BASE}/admin/memory/users/items/{mem_id}", headers=ah)
            ok_del = r.status_code == 200
        record("个人记忆 CRUD（add/list/delete）", ok_add and ok_list and ok_del)
        # 6. /skills 结构（10 工具 + dept_skills）
        mh = {"Authorization": f"Bearer {tok_m}"}
        r = await c.get(f"{BASE}/skills", headers=mh)
        d = r.json()
        # 4.1：10 基础工具 + html_report + 3 skill（ppt_master/framework/officecli）= 14
        # KB-REDESIGN：+ kb_read（知识库全文阅读）；B10：+ read_output（产出读取）= 16
        # v2（2026-08-14）：+ todo_step/ask_user/intent_event/result_event = 20；+ 子代理 = 22
        # 2026-08-18 修正：本接口（HTTP）按设计过滤 _INTERNAL_TOOLS（intent_event/result_event/
        # todo_step/ask_user 恒注入不展示，tools/__init__.py:123）→ 22-4=18。
        # 2026-08-21：内置三技能（framework/officecli/ppt_master）下线 → 18-3=15
        # 2026-09-09：示例站点 5 项（xhs_download + xhs_go_* 4 件套）合并为单一 xhs 录入机关 =16
        # 2026-09-10：kb_match/kb_read 下线 + file_search 上线（跨库检索统一入口）→ 16-2+1=15
        tools = d.get("tools", [])
        # 2026-09-17：计数随工具面变化更新（原 15 已过期：09-15 媒体两件套 / 09-17 下线 sql_query）
        ok_skills = len(tools) == 16 and isinstance(d.get("dept_skills"), list) \
            and all(t.get("user_description") for t in tools)
        record("skills 结构（16 工具 + dept_skills + user_description）", ok_skills, f"tools={len(tools)}")
        # 7. 团队技能上传（dept_admin）→ 员工可见 → 启停删除
        skill_md = ("---\nname: 测试技能\n"
                    "description: 测试用的团队技能。\n"
                    'tools: ["file_parse"]\n'
                    "---\n正文：按指令执行。")
        r = await c.post(f"{BASE}/skills/files", headers=dah, files={"file": ("test.md", skill_md.encode())})
        ok_up = r.status_code == 200
        skid = r.json().get("id") if ok_up else None
        r = await c.get(f"{BASE}/skills", headers=mh)
        ok_visible = any(s["id"] == skid for s in r.json().get("dept_skills", []))
        ok_dis = False
        if skid:
            r = await c.put(f"{BASE}/skills/files/{skid}", headers=dah, json={"status": "disabled"})
            ok_dis = r.status_code == 200
            await c.delete(f"{BASE}/skills/files/{skid}", headers=dah)
        record("团队技能上传/可见/启停", ok_up and ok_visible and ok_dis)
        # 8. 员工上传技能 403
        r = await c.post(f"{BASE}/skills/files", headers=mh, files={"file": ("test.md", skill_md.encode())})
        record("员工上传团队技能 403", r.status_code == 403, f"got {r.status_code}")

        # 9. run_script 兜底恒注入（skill_router）
        from app.agent.nodes.skill_router import run_skill_router
        from app.services.config_service import get_dept_tools

        cfg = {"configurable": {"events": None, "llm": None, "waiter": None, "ctx": None}}
        state = {"active_skills": ["file_parse"], "auto_skill": False, "user_role": "employee",
                 "department_id": "demo", "user_id": 2, "user_question": "t", "uploaded_files": [], "allowed_tools": None}
        r = await run_skill_router(state, cfg)
        ok_sandbox = "run_script" in r["allowed_tools"]
        record("run_script 恒注入（兜底沙盒）", ok_sandbox, f"allowed={r['allowed_tools']}")
        # 10. 工具合并（旧工具已移除，新参数就位）；2026-09-17：sql_query 已随数据查询线下线
        from app.agent.tools import get_tool

        t_file = get_tool("file_parse")
        t_mem = get_tool("memory")
        ok_merged = (
            t_file is not None and t_mem is not None
            and get_tool("db_schema") is None and get_tool("doc_parse") is None
            and get_tool("xlsx_parse") is None and get_tool("memory_add") is None
            and get_tool("sql_query") is None
            and "action" in t_mem.parameters["properties"]
        )
        record("旧工具已移除（db_schema/doc_parse/xlsx_parse/memory_add/sql_query）", ok_merged)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n=== {'PASS' if not failed else 'FAIL'}: {len(RESULTS) - len(failed)}/{len(RESULTS)} ===")
    if failed:
        print("失败项:", failed)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
