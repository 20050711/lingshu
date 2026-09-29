"""团队技能 zip 上传 + 沙盒执行冒烟（2026-08-20 新增）。

用法: cd backend && conda run -n aip python tests/skill_zip_smoke.py
覆盖：
1. parse_skill_zip 安全单测：zip slip（../、绝对路径、反斜杠、symlink）/ 超 5MB / 超 20MB / 超 200 文件
   / 缺根 SKILL.md / frontmatter 非法——全部抛 ValueError
2. 端到端：admin 登录 → 上传 zip（SKILL.md + 多目录脚本）→ 落盘 md/scripts 分目录 → 注入 prompt 含脚本清单
   → run_script 执行技能脚本（同团队成功 / 他团队拒绝）→ file_parse 读技能文件（同团队放行 / 他团队拒绝）
   → reupload 原子替换 → DELETE 清理目录
3. 纯 md 上传回归（skill_dir=NULL，无落盘）
自建自删（技能名带随机后缀），结束清理目录与库行。
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
from app.services.skill_file_service import build_skill_files_prompt, list_active_skills, parse_skill_zip

_settings = get_settings()
TAG = "zip_smoke"
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
name: zip-smoke
description: 技能包冒烟验证
---

# 指令
用 run_script 执行 scripts 下的 hello.py。
""".encode("utf-8")

# ===== 1. 安全单测 =====

def test_zip_security() -> None:
    cases: list[tuple[str, bytes, str]] = [
        ("缺根 SKILL.md", build_zip({"a.txt": b"x"}), "缺少 SKILL.md"),
        ("frontmatter 非法", build_zip({"SKILL.md": b"no frontmatter"}), "frontmatter"),
        ("路径 ../", build_zip({"SKILL.md": MD_OK, "../evil.sh": b"x"}), "非法成员路径"),
        ("绝对路径", build_zip({"SKILL.md": MD_OK, "/etc/evil.sh": b"x"}), "非法成员路径"),
        ("反斜杠穿越", build_zip({"SKILL.md": MD_OK, "..\\evil.sh": b"x"}), "非法成员路径"),
        ("超过 5MB", b"x" * (5 * 1024 * 1024 + 1), "超过"),
    ]
    for name, data, expect in cases:
        try:
            parse_skill_zip(data)
            check(f"安全:{name}", False, "应拒绝但通过了")
        except ValueError as e:
            check(f"安全:{name}", expect in str(e), str(e)[:60])


def test_zip_symlink() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", MD_OK)
        zi = zipfile.ZipInfo("link.sh")
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16  # type: ignore[attr-defined]
        z.writestr(zi, "/etc/passwd")
    try:
        parse_skill_zip(buf.getvalue())
        check("安全:symlink 成员", False, "应拒绝但通过了")
    except ValueError as e:
        check("安全:symlink 成员", "符号链接" in str(e), str(e)[:60])


# ===== 2. 端到端 =====

async def login_admin(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/auth/login", json={
        "department_id": "dept_root", "username": "admin", "password": pw("admin")})
    check("admin 登录", r.status_code == 200, f"HTTP {r.status_code}")


async def e2e() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await login_admin(client)

        # 上传 zip（根 SKILL.md + 多目录脚本）
        zip_bytes = build_zip({
            "SKILL.md": MD_OK,
            "hello.py": b"print('ZIP-SMOKE-OK', 1+1)\n",
            "util/helper.sh": b"#!/bin/bash\necho helper-ok\n",
        })
        r = await client.post("/api/v1/skills/files",
                              files={"file": ("skill.zip", zip_bytes, "application/zip")})
        check("上传 zip", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
        if r.status_code != 200:
            return
        sid = r.json()["id"]
        check("has_scripts", r.json().get("has_scripts") is True)

        # 落盘检查
        skill_dir = f"dept_root/{sid}"
        root = Path(_settings.skill_files_dir)
        md_f = root / skill_dir / "md" / "SKILL.md"
        py_f = root / skill_dir / "scripts" / "hello.py"
        sh_f = root / skill_dir / "scripts" / "util" / "helper.sh"
        check("落盘 md/SKILL.md", md_f.is_file())
        check("落盘 scripts/hello.py", py_f.is_file())
        check("落盘 scripts/util/helper.sh", sh_f.is_file())

        # DB skill_dir
        engine = get_global_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT skill_dir FROM skill_files WHERE id=:id"), {"id": sid})).first()
        check("DB skill_dir", bool(row and row[0] == skill_dir))

        # Q8（2026-08-26）：注入 prompt 只含技能清单（名字+简介）；脚本清单由 skill_read 按名获取
        skills = await list_active_skills("dept_root")
        prompt = await build_skill_files_prompt(skills, "dept_root")
        check("注入含技能清单", "zip-smoke" in prompt and f"{skill_dir}/scripts" not in prompt)
        from app.agent.tools.skill_read import run_skill_read

        sctx = ToolContext(f"zip-smoke-{sid}", 1, "dept_root", "employee", "smoke",
                           f"/data/outputs/zip-smoke-{sid}/1", user_id=1)
        sr = await run_skill_read({"name": "zip-smoke"}, sctx)
        check("skill_read 返回脚本清单", sr.get("has_scripts") and "hello.py" in str(sr.get("scripts")),
              str(sr.get("error") or "")[:60])

        # run_script 执行技能脚本（同团队）
        ctx = ToolContext(f"zip-smoke-{sid}", 1, "dept_root", "employee", "smoke",
                          f"/data/outputs/zip-smoke-{sid}/1", user_id=1)
        r = await run_script({"mode": "run", "lang": "python", "file": str(py_f)}, ctx)
        check("run_script 执行技能脚本", r.get("exit_code") == 0 and "ZIP-SMOKE-OK" in (r.get("stdout") or ""),
              (r.get("stderr") or r.get("error") or "")[:60])
        r = await run_script({"mode": "run", "lang": "bash", "file": str(sh_f)}, ctx)
        check("run_script 执行子目录脚本", r.get("exit_code") == 0 and "helper-ok" in (r.get("stdout") or ""))

        # 他团队：执行拒绝 + file_parse 拒绝
        ctx_other = ToolContext(f"zip-smoke-other-{sid}", 1, "demo", "employee", "smoke",
                                f"/data/outputs/zip-smoke-other-{sid}/1", user_id=1)
        r = await run_script({"mode": "run", "lang": "python", "file": str(py_f)}, ctx_other)
        check("他团队执行拒绝", "error" in r, (r.get("error") or "")[:50])
        r = await run_file_parse({"file_path": str(md_f)}, ctx_other)
        check("他团队 file_parse 拒绝", "error" in r, (r.get("error") or "")[:50])

        # 同团队 file_parse 放行
        r = await run_file_parse({"file_path": str(md_f)}, ctx)
        check("同团队 file_parse 放行", "error" not in r)

        # reupload 原子替换（换脚本内容）
        zip2 = build_zip({
            "SKILL.md": MD_OK,
            "hello.py": b"print('ZIP-SMOKE-V2')\n",
            "extra/new.py": b"print('extra')\n",
        })
        r = await client.post(f"/api/v1/skills/files/{sid}/reupload",
                              files={"file": ("skill.zip", zip2, "application/zip")})
        check("reupload zip", r.status_code == 200, f"HTTP {r.status_code} {r.text[:80]}")
        check("reupload 新文件落盘", (root / skill_dir / "scripts" / "extra" / "new.py").is_file())
        check("reupload 旧文件清理", not (root / skill_dir / "scripts" / "util" / "helper.sh").exists())
        r = await run_script({"mode": "run", "lang": "python", "file": str(root / skill_dir / "scripts" / "hello.py")}, ctx)
        check("reupload 后执行新脚本", "ZIP-SMOKE-V2" in (r.get("stdout") or ""))

        # 2026-08-20：沙盒可写——dept_admin 角色 run_script 修改技能文件（写 scripts 新文件 + 注入同步）
        ctx_admin = ToolContext(f"zip-smoke-admin-{sid}", 1, "dept_root", "dept_admin", "smoke",
                                f"/data/outputs/zip-smoke-admin-{sid}/1", user_id=1)
        r = await run_script({"mode": "run", "lang": "python",
                              "code": f"open('/data/skills/dept_root/{sid}/scripts/agent_write.txt','w').write('agent-writable')\nprint('W-OK')"},
                             ctx_admin)
        check("管理员沙盒可写技能文件", r.get("exit_code") == 0 and "W-OK" in (r.get("stdout") or ""),
              (r.get("stderr") or "")[:80])
        check("可写落盘", (root / skill_dir / "scripts" / "agent_write.txt").read_text() == "agent-writable")
        # 2026-08-20：员工（employee）只读——写技能文件必须失败
        r = await run_script({"mode": "run", "lang": "python",
                              "code": f"open('/data/skills/dept_root/{sid}/scripts/emp_write.txt','w').write('x')\nprint('E-OK')"},
                             ctx)
        check("员工写技能文件被拒", r.get("exit_code") != 0 or "E-OK" not in (r.get("stdout") or ""),
              f"exit={r.get('exit_code')} stderr={(r.get('stderr') or '')[:80]}")
        # 修改 SKILL.md → 下轮注入同步新内容（文件是源，DB 是缓存）
        md2 = (root / skill_dir / "md" / "SKILL.md").read_text(encoding="utf-8").replace(
            "技能包冒烟验证", "技能包冒烟验证-已修改")
        (root / skill_dir / "md" / "SKILL.md").write_text(md2, encoding="utf-8")
        skills2 = await list_active_skills("dept_root")
        p2 = await build_skill_files_prompt(skills2, "dept_root", "zip-smoke@verify")
        check("修改后注入同步", "已修改" in p2)
        # 损坏容错：写坏 SKILL.md → 跳过注入（2026-09-11：提示行改为**点名**技能+原因，运维可定位；
        # 技能正文/简介仍不被注入——这里用 description 断言，名字出现在原因行是预期行为）
        (root / skill_dir / "md" / "SKILL.md").write_text("broken no frontmatter", encoding="utf-8")
        skills3 = await list_active_skills("dept_root")
        p3 = await build_skill_files_prompt(skills3, "dept_root", "zip-smoke@verify")
        check("损坏容错跳过注入（点名技能+原因，正文不注入）",
              "技能包冒烟验证" not in p3 and "文件损坏" in p3 and "zip-smoke（文件损坏）" in p3)

        # 纯 md 上传回归（skill_dir=NULL 无落盘）
        r = await client.post("/api/v1/skills/files",
                              files={"file": ("plain.md",
                                              "---\nname: zip-smoke-md\ndescription: md 回归\n---\n\n# x\n指令。\n".encode("utf-8"),
                                              "text/markdown")})
        check("纯 md 上传", r.status_code == 200 and r.json().get("has_scripts") is False,
              f"HTTP {r.status_code} {r.text[:80]}")
        if r.status_code == 200:
            mid = r.json()["id"]
            async with engine.connect() as conn:
                row = (await conn.execute(
                    text("SELECT skill_dir FROM skill_files WHERE id=:id"), {"id": mid})).first()
            check("纯 md skill_dir NULL", row and row[0] is None)

        # 清理：删除两技能（库行 + 目录）
        mid = mid if "mid" in dir() else None
        for tid in (sid, mid):
            if tid:
                r = await client.delete(f"/api/v1/skills/files/{tid}")
                check(f"DELETE 技能 {tid}", r.status_code == 200, f"HTTP {r.status_code}")
        check("删除后目录清理", not (root / skill_dir).exists())
        await engine.dispose()


async def main() -> int:
    test_zip_security()
    test_zip_symlink()
    await e2e()
    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== skill_zip_smoke {'PASS' if not fails else 'FAIL'}: {len(RESULTS) - len(fails)}/{len(RESULTS)} ===")
    return 1 if fails else 0


if __name__ == "__main__":
    import stat  # noqa: E402（symlink 单测用）

    raise SystemExit(asyncio.run(main()))
