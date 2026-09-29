"""全局技能（默认AI技能，运维发布）冒烟测试（2026-08-21 新增）。

用法: cd backend && conda run -n aip python tests/global_skill_smoke.py
覆盖：
1. 权限：dept_admin 传 scope=global 上传 403；admin 上传 zip 成功（落盘 /data/skills/global/{id}/）
2. 员工可见：demo 员工 GET /skills/global-prefs 可见且 dept_enabled=true；GET /skills 的 dept_skills 不含全局技能
3. 三层交集三向验证（运维 status / 团队开关 dept_global_skills / 员工 g: 偏好——任一关闭则消失）
4. 沙盒：员工执行全局脚本 OK；dept_admin 写全局技能文件被拒（--ro-bind）；file_parse 读全局 md 放行
5. 团队隔离反转：全局技能对多团队可见可执行（区别于团队技能的他团队拒绝）
6. 清理：DELETE 全局技能（库行 + 目录）+ 恢复团队开关配置
自建自删，结束清理。
"""
import asyncio
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

import httpx
from sqlalchemy import text

from app.agent.tools import ToolContext
from app.agent.tools.script_sandbox import run_script
from app.agent.tools.search_tools import run_file_parse
from app.core.config import get_settings
from app.core.database import get_global_engine
from app.main import app
from app.services.skill_file_service import (
    build_skill_files_prompt, list_active_global_skills,
)

_settings = get_settings()
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def build_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


MD_OK = """---
name: global-smoke
description: 全局技能冒烟验证
---

# 指令
用 run_script 执行 scripts 下的 hello.py。
""".encode("utf-8")

ZIP_BYTES = build_zip({
    "SKILL.md": MD_OK,
    "hello.py": b"print('GLOBAL-SMOKE-OK', 2*21)\n",
})


async def login(client: httpx.AsyncClient, dept: str, username: str, password: str) -> bool:
    r = await client.post("/api/v1/auth/login", json={
        "department_id": dept, "username": username, "password": password})
    return r.status_code == 200


async def main() -> int:
    engine = get_global_engine()
    # demo 员工真实 user_id（三层交集验证按登录用户维度，勿硬编码 1）
    async with engine.connect() as conn:
        row = (await conn.execute(
            text("SELECT id FROM users WHERE username='demo' AND department_id='demo'"))).first()
    mkt_uid = int(row[0]) if row else 0
    if not mkt_uid:
        print("[FAIL] 未找到 demo 员工用户（seed 缺失？）")
        return 1
    check("demo 用户 id 定位", True, f"uid={mkt_uid}")
    # 前置清理：历史残留的 g: 偏好行（技能删除不清理偏好行——产品行为同 dept:，死行无害；
    # 测试需自清理保证 configured=false 断言成立）
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM user_skill_prefs WHERE user_id=:uid AND skill_id LIKE 'g:%'"), {"uid": mkt_uid})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # ===== 0. 登录三账号 =====
        check("admin 登录", await login(client, "dept_root", "admin", pw("admin")))
        check("demo 员工登录", await login(client, "demo", "demo", pw("demo")))
        check("demo_admin 登录", await login(client, "demo", "demo_admin", pw("demo_admin")))
        check("demo1 员工登录", await login(client, "demo1", "demo1", pw("demo1")))

        # ===== 1. 权限：dept_admin scope=global 403 / admin 上传成功 =====
        r = await client.post("/api/v1/skills/files",
                              files={"file": ("skill.zip", ZIP_BYTES, "application/zip")},
                              data={"scope": "global"})
        check("dept_admin scope=global 上传 403", r.status_code == 403, f"HTTP {r.status_code} {r.text[:80]}")

        await login(client, "dept_root", "admin", pw("admin"))
        r = await client.post("/api/v1/skills/files",
                              files={"file": ("skill.zip", ZIP_BYTES, "application/zip")},
                              data={"scope": "global"})
        check("admin 上传全局技能 zip", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
        if r.status_code != 200:
            return 1
        sid = r.json()["id"]
        check("has_scripts", r.json().get("has_scripts") is True)
        check("dept_id=global", r.json().get("dept_id") == "global", str(r.json()))

        # 落盘检查：/data/skills/global/{sid}/md/SKILL.md + scripts/hello.py
        root = Path(_settings.skill_files_dir)
        gdir = f"global/{sid}"
        md_f = root / gdir / "md" / "SKILL.md"
        py_f = root / gdir / "scripts" / "hello.py"
        check("落盘 global/md/SKILL.md", md_f.is_file())
        check("落盘 global/scripts/hello.py", py_f.is_file())
        async with engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT skill_dir FROM skill_files WHERE id=:id"), {"id": sid})).first()
        check("DB skill_dir=global/{id}", bool(row and row[0] == gdir))

        # ===== 2. 员工可见 =====
        await login(client, "demo", "demo", pw("demo"))
        r = await client.get("/api/v1/skills/global-prefs")
        d = r.json()
        mine = next((s for s in d.get("skills", []) if s["id"] == sid), None)
        check("员工 global-prefs 可见全局技能", r.status_code == 200 and mine is not None, f"HTTP {r.status_code}")
        check("dept_enabled=true（团队未配置=全部允许）", bool(mine and mine.get("dept_enabled") is True))
        check("configured=false（员工未配置偏好）", d.get("configured") is False)
        # 与团队技能隔离：GET /skills 的 dept_skills 不含全局技能
        r = await client.get("/api/v1/skills")
        check("GET /skills dept_skills 不含全局技能",
              all(s.get("id") != sid for s in r.json().get("dept_skills", [])))

        # ===== 3. 三层交集三向验证（任一关闭则注入消失）=====
        await login(client, "dept_root", "admin", pw("admin"))
        # a. 运维停用 → 消失
        r = await client.put(f"/api/v1/skills/files/{sid}", json={"status": "disabled"})
        check("运维停用 200", r.status_code == 200, f"HTTP {r.status_code}")
        active = await list_active_global_skills("demo", mkt_uid)
        check("停用后注入消失", all(s["id"] != sid for s in active))
        # b. 恢复 active + 团队全禁 → 消失
        await client.put(f"/api/v1/skills/files/{sid}", json={"status": "active"})
        r = await client.put("/api/v1/skills/global-dept", json={"enabled_ids": []}, params={"dept_id": "demo"})
        check("admin 设团队全禁 200", r.status_code == 200, f"HTTP {r.status_code}")
        active = await list_active_global_skills("demo", mkt_uid)
        check("团队全禁后注入消失", all(s["id"] != sid for s in active))
        # c. 恢复团队（全部允许）+ 员工个人全关 → 消失
        r = await client.put("/api/v1/skills/global-dept", json={"enabled_ids": None}, params={"dept_id": "demo"})
        check("恢复团队全部允许 200", r.status_code == 200, f"HTTP {r.status_code}")
        await login(client, "demo", "demo", pw("demo"))
        r = await client.put("/api/v1/skills/global-prefs", json={"enabled_ids": []})
        check("员工个人全关 200", r.status_code == 200, f"HTTP {r.status_code}")
        active = await list_active_global_skills("demo", mkt_uid)
        check("员工全关后注入消失", all(s["id"] != sid for s in active))
        # d. 员工恢复启用 → 出现
        r = await client.put("/api/v1/skills/global-prefs", json={"enabled_ids": [sid]})
        check("员工恢复启用 200", r.status_code == 200, f"HTTP {r.status_code}")
        active = await list_active_global_skills("demo", mkt_uid)
        check("员工启用后注入恢复", any(s["id"] == sid for s in active))
        # e. 团队管理员查/改本团队开关；跨团队被拒（先由 admin 设团队清单 [sid]，再验证 dept_admin 可查可改）
        await login(client, "dept_root", "admin", pw("admin"))
        await client.put("/api/v1/skills/global-dept", json={"enabled_ids": [sid]}, params={"dept_id": "demo"})
        await login(client, "demo", "demo_admin", pw("demo_admin"))
        r = await client.get("/api/v1/skills/global-dept")
        check("dept_admin 查本团队开关", r.status_code == 200 and r.json().get("configured") is True
              and r.json().get("enabled_ids") == [sid], f"HTTP {r.status_code} {r.text[:80]}")
        r = await client.get("/api/v1/skills/global-dept", params={"dept_id": "demo1"})
        check("dept_admin 查他团队开关 403", r.status_code == 403, f"HTTP {r.status_code}")

        # ===== 4. 沙盒 =====
        # 员工（demo）执行全局脚本 → OK
        await login(client, "demo", "demo", pw("demo"))
        ctx = ToolContext(f"global-smoke-{sid}", 1, "demo", "employee", "smoke",
                          f"/data/outputs/global-smoke-{sid}/1", user_id=1)
        r = await run_script({"mode": "run", "lang": "python", "file": str(py_f)}, ctx)
        check("员工执行全局脚本", r.get("exit_code") == 0 and "GLOBAL-SMOKE-OK" in (r.get("stdout") or ""),
              (r.get("stderr") or r.get("error") or "")[:80])
        # dept_admin 角色写全局技能文件 → 被拒（全局所有角色只读 --ro-bind）
        ctx_da = ToolContext(f"global-smoke-da-{sid}", 1, "demo", "dept_admin", "smoke",
                             f"/data/outputs/global-smoke-da-{sid}/1", user_id=1)
        r = await run_script({"mode": "run", "lang": "python",
                              "code": f"open('/data/skills/{gdir}/scripts/da_write.txt','w').write('x')\nprint('DA-W-OK')"},
                             ctx_da)
        check("dept_admin 写全局技能文件被拒", r.get("exit_code") != 0 or "DA-W-OK" not in (r.get("stdout") or ""),
              f"exit={r.get('exit_code')} stderr={(r.get('stderr') or '')[:80]}")
        check("被拒未落盘", not (root / gdir / "scripts" / "da_write.txt").exists())
        # 员工 file_parse 读全局 md → 放行
        r = await run_file_parse({"file_path": str(md_f)}, ctx)
        check("员工 file_parse 读全局 md 放行", "error" not in r, (r.get("error") or "")[:60])

        # ===== 5. 团队隔离反转：全局技能对 demo1 也可见可执行 =====
        await login(client, "demo1", "demo1", pw("demo1"))
        r = await client.get("/api/v1/skills/global-prefs")
        check("demo1 员工可见全局技能", any(s["id"] == sid for s in r.json().get("skills", [])))
        ctx_sales = ToolContext(f"global-smoke-demo1-{sid}", 1, "demo1", "employee", "smoke",
                                f"/data/outputs/global-smoke-demo1-{sid}/1", user_id=1)
        r = await run_script({"mode": "run", "lang": "python", "file": str(py_f)}, ctx_sales)
        check("demo1 员工执行全局脚本", r.get("exit_code") == 0 and "GLOBAL-SMOKE-OK" in (r.get("stdout") or ""),
              (r.get("stderr") or r.get("error") or "")[:80])

        # Q8（2026-08-26）：注入 prompt 只含技能清单（名字+简介）；脚本清单由 skill_read 按名获取
        gs = await list_active_global_skills("demo", 1)
        prompt = await build_skill_files_prompt(gs, "global", "global-smoke@verify",
                                                section_title="全局技能", max_total_chars=1500)
        check("注入含全局技能段", "## 全局技能" in prompt and "global-smoke" in prompt and f"{gdir}/scripts" not in prompt)
        from app.agent.tools.skill_read import run_skill_read

        sctx = ToolContext(f"global-smoke-sr-{sid}", 1, "demo", "employee", "smoke",
                           f"/data/outputs/global-smoke-sr-{sid}/1", user_id=1)
        sr = await run_skill_read({"name": "global-smoke"}, sctx)
        check("skill_read 返回全局脚本清单", sr.get("has_scripts") and "hello.py" in str(sr.get("scripts")),
              str(sr.get("error") or "")[:60])

        # ===== 6. 清理 =====
        await login(client, "dept_root", "admin", pw("admin"))
        # 恢复团队开关为未配置（防污染其他测试）+ 清 demo 员工 g: 偏好行
        await client.put("/api/v1/skills/global-dept", json={"enabled_ids": None}, params={"dept_id": "demo"})
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM user_skill_prefs WHERE user_id=:uid AND skill_id LIKE 'g:%'"), {"uid": mkt_uid})
        r = await client.delete(f"/api/v1/skills/files/{sid}")
        check("DELETE 全局技能", r.status_code == 200, f"HTTP {r.status_code}")
        check("删除后目录清理", not (root / gdir).exists())
        await engine.dispose()

    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== global_skill_smoke {'PASS' if not fails else 'FAIL'}: {len(RESULTS) - len(fails)}/{len(RESULTS)} ===")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
