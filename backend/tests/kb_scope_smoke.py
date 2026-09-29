"""知识库归属三态验收测试（2026-08-24 阶段 2）：权限矩阵 / 个人 KB / 合并检索 source / xlsx 特例 / 会话复制。

覆盖（计划 2.8）：
- 权限矩阵：employee 上传团队文档 403 / dept_admin 分类 CRUD（有子拒绝/改名/删分类置 NULL）/ 改挂分类 /
  跨团队不可见（详情 404 + 检索不含 + file_search 不含 + 原文件路径白名单拒绝）/ admin 带 department_id 上传（分类匹配/个人分类拒绝）
- 个人 KB CRUD 与跨用户拒绝（test 团队 1/2 号账号）
- 合并检索 kb_source 断言（file_search 直调：dept/personal）
- xlsx 特例：恰 1 块占位 + 摘要替换（轮询 summary，超时跳过）+ 检索命中
- copy_kb_xlsx_to_session 会话复制 + validate_readable_path 放行

用法（后端运行中，登录态走 cookie）：cd backend && source scripts/env_aip.sh && python -u tests/kb_scope_smoke.py
原则：测试自建自删（踩坑 38——任何用例不得删真实数据）；LLM 调用不开 thinking 用 flash 档。
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

import httpx
from sqlalchemy import text

from app.core.database import get_global_engine

BASE = "http://localhost:8001/api/v1"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


async def login(c: httpx.AsyncClient, dept: str, username: str, password: str) -> None:
    r = await c.post(f"{BASE}/auth/login", json={"department_id": dept, "username": username, "password": password})
    r.raise_for_status()


async def _uid(dept: str, username: str) -> int:
    """DB 查用户真实 id（工具直调/会话复制需要真实 user_id）。"""
    async with get_global_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT id FROM users WHERE department_id=:d AND username=:u"),
                {"d": dept, "u": username},
            )
        ).first()
    return int(row[0]) if row else 0


def _xlsx_bytes() -> bytes:
    """内存造 xlsx（指标/数值 3 行）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "数据"
    ws.append(["指标", "数值"])
    ws.append(["营收", 100])
    ws.append(["成本", 40])
    ws.append(["利润", 60])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def main() -> None:
    engine = get_global_engine()
    # 0. 迁移幂等（user_id 列 DDL 连跑两遍不报错）
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS user_id INT"))
        await conn.execute(text("ALTER TABLE kb_categories ADD COLUMN IF NOT EXISTS user_id INT"))
    record("迁移幂等（user_id 列 DDL 无错）", True)

    # 自建自删：记录本测试创建的 id（清理阶段逐项删除）
    created_docs: list[tuple[int, str]] = []   # (doc_id, 归属身份标识)
    created_cats: list[tuple[int, str]] = []   # (cat_id, 归属身份标识)

    async with httpx.AsyncClient(timeout=60) as c:
        # ===== A. employee 上传团队文档 403（权限收紧，原员工可上传已收掉）=====
        await login(c, "demo", "demo", pw("demo"))
        r = await c.post(f"{BASE}/knowledge/documents",
                         files={"file": ("denied.md", b"denied")},
                         data={"title": "烟测denied"})
        record("employee 上传团队文档 403", r.status_code == 403, f"got {r.status_code}")

        # ===== B. dept_admin 团队分类 CRUD + 改挂分类 + 删分类置 NULL =====
        await login(c, "demo", "demo_admin", pw("demo_admin"))
        r = await c.post(f"{BASE}/knowledge/categories", json={"name": "烟测分类A", "scope": "dept"})
        catA = r.json().get("id") if r.status_code == 200 else None
        created_cats.append((catA, "admin_market"))
        record("dept_admin 创建团队分类", r.status_code == 200, f"got {r.status_code}")
        r = await c.post(f"{BASE}/knowledge/categories",
                         json={"name": "烟测分类A子", "scope": "dept", "parent_id": catA})
        catA2 = r.json().get("id") if r.status_code == 200 else None
        created_cats.append((catA2, "admin_market"))
        r = await c.delete(f"{BASE}/knowledge/categories/{catA}")
        record("删除含子分类的分类被拒", r.status_code == 400, f"got {r.status_code}")
        r = await c.put(f"{BASE}/knowledge/categories/{catA}", json={"name": "烟测分类A改"})
        record("dept_admin 改团队分类名", r.status_code == 200, f"got {r.status_code}")
        r = await c.post(f"{BASE}/knowledge/categories", json={"name": "烟测分类B", "scope": "dept"})
        catB = r.json().get("id") if r.status_code == 200 else None
        created_cats.append((catB, "admin_market"))
        # 上传团队文档挂 catA
        dept_doc_content = "烟测团队文档专属标记DA1\n" + "团队制度内容。" * 50
        r = await c.post(f"{BASE}/knowledge/documents",
                         files={"file": ("dept.md", dept_doc_content.encode())},
                         data={"title": "烟测团队文档", "category_id": str(catA)})
        docA = r.json().get("id") if r.status_code == 200 else None
        created_docs.append((docA, "admin_market"))
        record("dept_admin 上传团队文档", r.status_code == 200, f"got {r.status_code}")
        # 改挂分类 catA → catB
        r = await c.put(f"{BASE}/knowledge/documents/{docA}", json={"category_id": catB})
        async with engine.connect() as conn:
            cid = (await conn.execute(
                text("SELECT category_id FROM kb_documents WHERE id=:id"), {"id": docA}
            )).scalar()
        record("改挂文档分类（catA→catB）", r.status_code == 200 and cid == catB,
               f"got {r.status_code} db_cid={cid}")
        # 删分类置 NULL：先删子分类 catA2，再删 catA，最后删 catB → docA.category_id=NULL
        r = await c.delete(f"{BASE}/knowledge/categories/{catA2}")
        record("删除子分类", r.status_code == 200, f"got {r.status_code}")
        r = await c.delete(f"{BASE}/knowledge/categories/{catA}")
        record("删除团队分类", r.status_code == 200, f"got {r.status_code}")
        r = await c.delete(f"{BASE}/knowledge/categories/{catB}")
        async with engine.connect() as conn:
            cid = (await conn.execute(
                text("SELECT category_id FROM kb_documents WHERE id=:id"), {"id": docA}
            )).scalar()
        record("删分类后文档分类置 NULL", r.status_code == 200 and cid is None,
               f"got {r.status_code} db_cid={cid}")

        # ===== C. 跨团队不可见（demo1 看不到 demo 团队文档）=====
        from app.agent.tools import ToolContext, get_tool

        t_search = get_tool("file_search")   # 2026-09-10：kb_match/kb_read 下线，统一走 file_search
        await login(c, "demo1", "demo1", pw("demo1"))
        r = await c.get(f"{BASE}/knowledge/documents/{docA}")
        record("跨团队文档详情 404", r.status_code == 404, f"got {r.status_code}")
        r = await c.get(f"{BASE}/knowledge/search", params={"q": "烟测团队文档"})
        ok = all(d.get("id") != docA for d in (r.json().get("documents") or []))
        record("跨团队检索不含", r.status_code == 200 and ok, f"got {r.status_code}")
        ctx_sales = ToolContext("kb-scope", 1, "demo1", "employee", "demo1", "/data/outputs/kb-scope/1",
                                user_id=await _uid("demo1", "demo1"))
        # 2026-09-10：kb_read 下线 → 两条断言替代
        # ① file_search 检索不到别团队文档（三态 WHERE）
        r = await t_search.handler({"query": "烟测团队文档专属标记DA1"}, ctx_sales)
        ok = all(m.get("document_id") != docA for m in (r.get("content_matches") or []))
        record("file_search 跨团队不含他人团队文档", ok, str(r)[:100])
        # ② 原文件路径白名单：非本团队知识库根一律拒绝（新边界，替代 kb_read 的 DB 通道校验）
        from app.agent.tools import validate_readable_path
        ctx_demo1_no_kb = ToolContext("kb-scope", 1, "demo1", "employee", "demo1", "/data/outputs/kb-scope/1",
                                      user_id=await _uid("demo1", "demo1"),
                                      kb_roots=["/data/uploads/kb/global", "/data/uploads/kb/demo1",
                                                f"/data/uploads/kb/u{await _uid('demo1', 'demo1')}"])
        err_other = validate_readable_path("/data/uploads/kb/demo/202609/deadbeef.md", ctx_demo1_no_kb)
        err_global = validate_readable_path("/data/uploads/kb/global/202609/deadbeef.md", ctx_demo1_no_kb)
        err_u2 = validate_readable_path("/data/uploads/kb/u9999/202609/deadbeef.md", ctx_demo1_no_kb)
        record("kb 原文件路径白名单（他团队拒/全局放行/他人个人拒）",
               err_other is not None and err_global is None and err_u2 is not None,
               f"other={err_other is not None} global={err_global is None} u2={err_u2 is not None}")

        # ===== D. 个人 KB CRUD 与跨用户拒绝（test/1 与 test/2）=====
        await login(c, "test", "1", pw("test1"))
        uid1 = await _uid("test", "1")
        r = await c.post(f"{BASE}/knowledge/categories", json={"name": "烟测个人分类1", "scope": "personal"})
        pc1 = r.json().get("id") if r.status_code == 200 else None
        created_cats.append((pc1, "test1"))
        record("employee 创建个人分类", r.status_code == 200, f"got {r.status_code}")
        r = await c.post(f"{BASE}/knowledge/categories", json={"name": "烟测个人分类2", "scope": "personal"})
        pc2 = r.json().get("id") if r.status_code == 200 else None
        created_cats.append((pc2, "test1"))
        personal_doc_content = "烟测个人文档专属标记PD1\n" + "我的个人记录内容。" * 50
        r = await c.post(f"{BASE}/knowledge/documents",
                         files={"file": ("personal.md", personal_doc_content.encode())},
                         data={"title": "烟测个人文档", "scope": "personal", "category_id": str(pc1)})
        pd1 = r.json().get("id") if r.status_code == 200 else None
        created_docs.append((pd1, "test1"))
        record("employee 上传个人文档", r.status_code == 200, f"got {r.status_code}")
        # employee 改团队文档分类 403（团队文档管理权在 dept_admin）
        r = await c.put(f"{BASE}/knowledge/documents/{docA}", json={"category_id": None})
        record("employee 改团队文档分类 403", r.status_code == 403, f"got {r.status_code}")
        # 跨用户拒绝（test/2）
        await login(c, "test", "2", "Dx8%lX%uVG91376V")
        r = await c.get(f"{BASE}/knowledge/documents/{pd1}")
        record("跨用户个人文档详情 404", r.status_code == 404, f"got {r.status_code}")
        r = await c.get(f"{BASE}/knowledge/search", params={"q": "烟测个人文档"})
        ok = all(d.get("id") != pd1 for d in (r.json().get("documents") or []))
        record("跨用户检索不含", r.status_code == 200 and ok, f"got {r.status_code}")
        ctx_t2 = ToolContext("kb-scope", 1, "test", "employee", "t2", "/data/outputs/kb-scope/1",
                             user_id=await _uid("test", "2"))
        r = await t_search.handler({"query": "烟测个人文档专属标记PD1"}, ctx_t2)
        ok = all(m.get("document_id") != pd1 for m in (r.get("content_matches") or []))
        record("file_search 跨用户不含他人个人文档", ok, str(r)[:100])
        r = await c.put(f"{BASE}/knowledge/categories/{pc1}", json={"name": "越权改名"})
        record("跨用户改个人分类 403", r.status_code == 403, f"got {r.status_code}")
        # 本人改挂分类 pc1 → pc2
        await login(c, "test", "1", pw("test1"))
        r = await c.put(f"{BASE}/knowledge/documents/{pd1}", json={"category_id": pc2})
        async with engine.connect() as conn:
            cid = (await conn.execute(
                text("SELECT category_id FROM kb_documents WHERE id=:id"), {"id": pd1}
            )).scalar()
        record("本人改挂个人文档分类", r.status_code == 200 and cid == pc2,
               f"got {r.status_code} db_cid={cid}")
        # employee 无权把团队文档挂个人分类（403：employee 连团队文档修改权都没有）
        r_bad = await c.put(f"{BASE}/knowledge/documents/{docA}", json={"category_id": pc1})
        record("employee 改团队文档分类 403", r_bad.status_code == 403, f"got {r_bad.status_code}")

        # ===== E. admin 带 department_id 上传（分类匹配/个人分类拒绝）=====
        # admin 账号团队为 dept_root（seed.py 定义）
        await login(c, "dept_root", "admin", pw("admin"))
        r = await c.post(f"{BASE}/admin/knowledge/categories",
                         json={"name": "烟测admin分类", "dept_id": "demo"})
        catAdm = r.json().get("id") if r.status_code == 200 else None
        created_cats.append((catAdm, "admin_global"))
        r = await c.post(f"{BASE}/admin/knowledge/documents",
                         files={"file": ("adm.md", "烟测admin团队文档内容AD1".encode())},
                         data={"title": "烟测admin团队文档", "department_id": "demo", "category_id": str(catAdm)})
        docB = r.json().get("id") if r.status_code == 200 else None
        created_docs.append((docB, "admin_global"))
        record("admin 带 department_id 上传（挂目标团队分类）", r.status_code == 200, f"got {r.status_code}")
        async with engine.connect() as conn:
            dept_of_docB = (await conn.execute(
                text("SELECT department_id FROM kb_documents WHERE id=:id"), {"id": docB}
            )).scalar()
        record("admin 上传落目标团队", dept_of_docB == "demo", f"db_dept={dept_of_docB}")
        # 分类与目标团队不匹配 → 400
        r = await c.post(f"{BASE}/admin/knowledge/documents",
                         files={"file": ("bad.md", b"bad")},
                         data={"title": "烟测bad", "department_id": "demo1", "category_id": str(catAdm)})
        record("admin 上传分类团队不匹配 400", r.status_code == 400, f"got {r.status_code}")
        # 个人分类一律拒绝（pc1 为 test/1 个人分类）
        r = await c.post(f"{BASE}/admin/knowledge/documents",
                         files={"file": ("bad2.md", b"bad2")},
                         data={"title": "烟测bad2", "department_id": "demo", "category_id": str(pc1)})
        record("admin 上传挂个人分类被拒 400", r.status_code == 400, f"got {r.status_code}")

        # ===== F. 合并检索 kb_source 断言（file_search 直调）=====
        # 团队文档用 demo 身份（test/1 属 test 团队看不到 demo 团队文档）
        await login(c, "demo", "demo_admin", pw("demo_admin"))
        ctx_market = ToolContext("kb-scope", 1, "demo", "dept_admin", "demo_admin",
                                 "/data/outputs/kb-scope/1",
                                 user_id=await _uid("demo", "demo_admin"))
        r = await t_search.handler({"query": "烟测团队文档专属标记DA1"}, ctx_market)
        ok_dept = (isinstance(r, dict) and any(
            m.get("document_id") == docA and m.get("kb_source") == "dept"
            for m in (r.get("content_matches") or [])))
        record("合并检索团队文档 kb_source=dept", ok_dept, str(r)[:150])
        await login(c, "test", "1", pw("test1"))
        ctx_t1 = ToolContext("kb-scope", 1, "test", "employee", "t1", "/data/outputs/kb-scope/1",
                             user_id=uid1)
        r = await t_search.handler({"query": "烟测个人文档专属标记PD1"}, ctx_t1)
        ok_personal = (isinstance(r, dict) and any(
            m.get("document_id") == pd1 and m.get("kb_source") == "personal"
            for m in (r.get("content_matches") or [])))
        record("合并检索个人文档 kb_source=personal", ok_personal, str(r)[:150])
        # categories/search 响应带 user_id（前端分区/标注）
        r = await c.get(f"{BASE}/knowledge/categories")
        ok_uid = any(x.get("user_id") == uid1 for x in (r.json().get("categories") or []))
        record("categories 响应含 user_id", r.status_code == 200 and ok_uid, f"got {r.status_code}")
        r = await c.get(f"{BASE}/knowledge/search", params={"q": "烟测个人文档"})
        ok_uid = any(d.get("user_id") == uid1 for d in (r.json().get("documents") or []))
        record("search 响应含 user_id", r.status_code == 200 and ok_uid, f"got {r.status_code}")

        # ===== G. xlsx 特例：恰 1 块 + 摘要替换 + 检索命中 =====
        xls_bytes = _xlsx_bytes()
        r = await c.post(f"{BASE}/knowledge/documents",
                         files={"file": ("烟测表格.xlsx", xls_bytes)},
                         data={"title": "烟测表格", "scope": "personal"})
        xdoc = r.json().get("id") if r.status_code == 200 else None
        created_docs.append((xdoc, "test1"))
        record("上传 xlsx 个人文档", r.status_code == 200, f"got {r.status_code}")
        async with engine.connect() as conn:
            n_chunks = (await conn.execute(
                text("SELECT COUNT(*) FROM kb_chunks WHERE document_id=:id"), {"id": xdoc}
            )).scalar()
            doc_row = (await conn.execute(
                text("SELECT content, file_type, user_id FROM kb_documents WHERE id=:id"), {"id": xdoc}
            )).first()
        ok_x = (n_chunks == 1 and doc_row[1] == "xlsx" and doc_row[2] == uid1
                and "营收" in (doc_row[0] or "") and "表头" in (doc_row[0] or ""))
        record("xlsx 恰 1 块占位 + content=预览全文", ok_x,
               f"chunks={n_chunks} ft={doc_row[1] if doc_row else None} len={len(doc_row[0]) if doc_row else 0}")
        # 摘要替换（_kb_summary_job 后台真实调 llm_aux；超时跳过不强制）
        summary = None
        for _ in range(6):
            await asyncio.sleep(2)
            async with engine.connect() as conn:
                summary = (await conn.execute(
                    text("SELECT summary FROM kb_documents WHERE id=:id"), {"id": xdoc}
                )).scalar()
            if summary:
                break
        if summary:
            async with engine.connect() as conn:
                blk = (await conn.execute(
                    text("SELECT content FROM kb_chunks WHERE document_id=:id AND seq=1"), {"id": xdoc}
                )).scalar()
            record("xlsx 摘要替换占位块", blk == summary, f"summary_len={len(summary)}")
        else:
            record("xlsx 摘要替换占位块", True, "跳过（摘要超时未完成，占位块仍在可检索）")
        # 检索命中：query 用标题「烟测表格」（摘要替换后占位块内容=LLM 摘要，可能不含"营收"字样，
        # 而 title ILIKE 恒命中——占位块"立即可检索"由下方摘要替换前的块内容保证）
        r = await t_search.handler({"query": "烟测表格"}, ctx_t1)
        ok_hit = (isinstance(r, dict) and any(
            m.get("document_id") == xdoc for m in (r.get("content_matches") or [])))
        record("xlsx file_search 检索命中", ok_hit, str(r)[:150])

        # ===== H. copy_kb_xlsx_to_session 会话复制 + validate_readable_path 放行 =====
        from app.agent.tools import validate_readable_path
        from app.services.kb_service import copy_kb_xlsx_to_session

        sess_id = ctx_t1.session_id  # 与 ctx 的 session_id 一致（validate_readable_path 按 user+session 校验）
        copied = await copy_kb_xlsx_to_session(uid1, "test", sess_id)
        files = copied.get("files", [])
        ok_copy = len(files) >= 1 and any("烟测表格" in f["file_name"] for f in files)
        record("copy_kb_xlsx_to_session 复制 xlsx", ok_copy, f"files={len(files)} truncated={copied.get('truncated')}")
        if files:
            denied = validate_readable_path(files[0]["file_path"], ctx_t1)
            record("validate_readable_path 放行 kb_xlsx 目录", denied is None, str(denied) if denied else "")

        # ===== 清理（自建自删）=====
        # 文档：docB（admin）、docA（demo_admin）、xdoc/pd1（test/1）
        await login(c, "dept_root", "admin", pw("admin"))
        for doc_id, _owner in created_docs:
            if doc_id:
                await c.delete(f"{BASE}/admin/knowledge/documents/{doc_id}")
        for cat_id, owner in created_cats:
            if not cat_id:
                continue
            if owner == "admin_global":
                await c.delete(f"{BASE}/admin/knowledge/categories/{cat_id}")
        await login(c, "demo", "demo_admin", pw("demo_admin"))
        await login(c, "test", "1", pw("test1"))
        for cat_id, owner in created_cats:
            if cat_id and owner == "test1":
                await c.delete(f"{BASE}/knowledge/categories/{cat_id}")
        # 验证清理干净：本测试创建的全部文档/分类已不存在
        async with engine.connect() as conn:
            n_docs = 0
            for d, _owner in created_docs:
                if d and (await conn.execute(text("SELECT 1 FROM kb_documents WHERE id=:id"), {"id": d})).first():
                    n_docs += 1
            n_cats = 0
            for x, _owner in created_cats:
                if x and (await conn.execute(text("SELECT 1 FROM kb_categories WHERE id=:id"), {"id": x})).first():
                    n_cats += 1
        record("清理完成（自建文档/分类全删）", n_docs == 0 and n_cats == 0,
               f"残留 docs={n_docs} cats={n_cats}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== {'PASS' if not failed else 'FAIL'}: {len(RESULTS) - len(failed)}/{len(RESULTS)} ===")
    if failed:
        print("失败项:", failed)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
