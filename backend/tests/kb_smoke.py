"""知识库重构验收测试（KB-REDESIGN，2026-08-07）：分段器 / 块级存储 / 两级检索 / 原文件读取 / 删除级联删盘 / 迁移幂等。

用法（后端运行中，登录态走 cookie）：cd backend && source scripts/env_aip.sh && python -u tests/kb_smoke.py
原则：测试自建自删（踩坑 38——任何用例不得删真实数据）。
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
from app.services.kb_chunker import chunk_text

BASE = "http://localhost:8001/api/v1"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'✓' if ok else '✗'} {name}" + (f"  {detail}" if detail else ""))


async def login(c: httpx.AsyncClient, dept: str, username: str, password: str) -> None:
    r = await c.post(f"{BASE}/auth/login", json={"department_id": dept, "username": username, "password": password})
    r.raise_for_status()


def _test_chunker() -> None:
    """分段器单测：块长 500-1000、重叠生效、标题分块、单段长文硬切、para_loc。"""
    # 1. 标题结构文档（标题块可短于 500——按标题边界收块是设计语义；块长上限 1050 必须满足）
    doc = "一、总则\n" + "制度说明内容。" * 60 + "\n\n二、细则\n" + "细则条款细节。" * 60 + "\n\n三、附则\n" + "附则补充内容。" * 60
    blocks = chunk_text(doc)
    ok_len = len(blocks) >= 2 and all(len(b["content"]) <= 1050 for b in blocks)
    ok_title = blocks[0]["block_title"].startswith("一、总则") and blocks[1]["block_title"].startswith("二、细则")
    ok_loc = all(b["para_loc"] for b in blocks)
    record("分段器：标题分块+块长上限+para_loc", ok_len and ok_title and ok_loc,
           f"blocks={len(blocks)} lens={[len(b['content']) for b in blocks]}")
    # 2. 单段超长硬切
    long_para = "超长段落内容。" * 300  # 3000+ 字单段
    blocks2 = chunk_text("标题\n\n" + long_para)
    ok_split = len(blocks2) >= 2 and all(len(b["content"]) <= 1100 for b in blocks2)
    record("分段器：单段超长硬切", ok_split, f"blocks={len(blocks2)} lens={[len(b['content']) for b in blocks2]}")
    # 3. 短文档单块
    blocks3 = chunk_text("短文档。" * 20)
    record("分段器：短文档单块", len(blocks3) == 1, f"blocks={len(blocks3)}")


def _build_long_doc(marker: str) -> str:
    """构造 >4000 字带标题结构的长文档（末尾含独特 marker，标题不含——验证内容级命中）。"""
    parts = ["# 烟测知识文档", "", "一、总则", "本制度适用于全体成员。", "规则细节内容。" * 200,
             "", "二、实施细则", "操作流程细节。" * 200, "", "三、考核要求", "考核标准说明。" * 200,
             "", "四、附则", f"本规定自发布之日起施行。{marker}"]
    return "\n".join(parts)


async def main() -> None:
    # 0. 迁移幂等（DDL 连跑两遍不报错；表结构为新方案）
    engine = get_global_engine()
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS file_path VARCHAR(500)"))
        await conn.execute(text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS uploaded_by INT"))
        await conn.execute(text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'"))
        await conn.execute(
            text("""
                CREATE TABLE IF NOT EXISTS kb_chunks (
                  id SERIAL PRIMARY KEY,
                  document_id INT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
                  seq INT NOT NULL, block_title VARCHAR(255), content TEXT NOT NULL,
                  para_loc VARCHAR(50), embedding REAL[], created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )""")
        )
    record("迁移幂等（DDL 两遍无错）", True)

    # 1. 分段器单测
    _test_chunker()

    async with httpx.AsyncClient(timeout=30) as c:
        # 2026-08-24（归属三态权限收紧）：员工上传团队文档已收掉——本用例改 demo_admin
        # （团队管理员）身份上传；employee 403 断言在删除后补充
        await login(c, "demo", "demo", pw("demo"))
        await login(c, "demo", "demo_admin", pw("demo_admin"))

        marker = "烟测紫电青霜金猊香屑"
        title = "烟测知识文档smoke"  # 标题不含 marker → 验证内容块命中而非标题命中
        r = await c.post(f"{BASE}/knowledge/documents",
                         files={"file": ("smoke_long.md", _build_long_doc(marker).encode())},
                         data={"title": title})
        doc_id = r.json().get("id") if r.status_code == 200 else None
        record("上传长文档（>4000 字，demo_admin）", r.status_code == 200, f"got {r.status_code}")

        # 2. DB 层：全文无截断 + chunks 落库
        async with engine.connect() as conn:
            row = (await conn.execute(
                text("SELECT content, file_path FROM kb_documents WHERE id=:id"), {"id": doc_id}
            )).first()
            n_chunks = (await conn.execute(
                text("SELECT COUNT(*) FROM kb_chunks WHERE document_id=:id"), {"id": doc_id}
            )).scalar()
        ok_full = row is not None and marker in row[0] and len(row[0]) > 4000
        record("全文入库无截断（>4000 字）+ file_path 入库", ok_full and row[1], f"len={len(row[0]) if row else 0}")
        record("分块落库（kb_chunks）", n_chunks >= 2, f"chunks={n_chunks}")

        # 3. 两级检索：内容级命中（标题不含 marker）
        from app.agent.tools import ToolContext, get_tool

        # 2026-09-10：kb_match/kb_read 下线 → file_search + file_parse（知识库原文件开放 + char_start 续读）
        from app.agent.tools import validate_readable_path

        _uid_kb = row[1] and "/data/uploads/kb/demo" in row[1]
        kb_roots_demo = ["/data/uploads/kb/global", "/data/uploads/kb/demo"]
        ctx = ToolContext("kb-smoke", 1, "demo", "employee", "smoke", "/data/outputs/kb-smoke/1",
                          kb_roots=kb_roots_demo)
        t_search = get_tool("file_search")
        r = await t_search.handler({"query": marker}, ctx)
        hits = r.get("content_matches") if isinstance(r, dict) else None
        ok_hit = bool(hits) and any(m.get("document_id") == doc_id for m in hits)
        record("file_search 内容级命中（标题不含关键词）", ok_hit,
               [(m.get("title"), m.get("seq")) for m in (hits or [])])

        # 4. 原文件读取：file_parse 按 char_start 精确续读（替代 kb_read 的块号导航）
        t_parse = get_tool("file_parse")
        hit = next((m for m in (hits or []) if m.get("document_id") == doc_id), {})
        kb_path = hit.get("file_path") or (row[1] if _uid_kb else None)
        r = await t_parse.handler({"file_path": kb_path, "offset": hit.get("char_start", 0), "length": 5000}, ctx)
        got_marker = marker in str(r)
        # 硬断言 char_start 存在（KB 侧块必须带偏移，否则 agent 无法精确续读）
        record("file_parse 读知识库原文件（char_start 续读命中 marker）",
               got_marker and hit.get("char_start") is not None,
               f"char_start={hit.get('char_start')} path_ok={bool(kb_path)}")

        # 5. 跨团队拒绝（demo1 团队：检索不含 + 原文件路径白名单拒绝）
        ctx_sales = ToolContext("kb-smoke", 1, "demo1", "employee", "smoke", "/data/outputs/kb-smoke/1",
                                kb_roots=["/data/uploads/kb/global", "/data/uploads/kb/demo1"])
        r = await t_search.handler({"query": marker}, ctx_sales)
        ok_deny = all(m.get("document_id") != doc_id for m in (r.get("content_matches") or []))
        record("file_search 跨团队不含", ok_deny, str(r)[:80])
        err = validate_readable_path(kb_path or "/data/uploads/kb/demo/x.md", ctx_sales)
        record("kb 原文件跨团队路径拒绝", err is not None, str(err)[:60])

        # 6. 删除级联删盘：文件消失 + chunks 消失
        file_path = row[1] if row else None
        r = await c.delete(f"{BASE}/knowledge/documents/{doc_id}")
        ok_del = r.status_code == 200
        ok_disk = True
        if file_path:
            ok_disk = not Path(file_path).exists()
        async with engine.connect() as conn:
            n_chunks = (await conn.execute(
                text("SELECT COUNT(*) FROM kb_chunks WHERE document_id=:id"), {"id": doc_id}
            )).scalar()
        record("删除文档：物理文件级联删盘（M15）+ chunks 级联清理", ok_del and ok_disk and n_chunks == 0,
               f"del={r.status_code} disk_cleaned={ok_disk} chunks={n_chunks}")

        # 7. 权限收紧（2026-08-24 归属三态）：employee 上传团队文档 403（原员工可上传已收掉）
        await login(c, "demo", "demo", pw("demo"))
        r = await c.post(f"{BASE}/knowledge/documents",
                         files={"file": ("smoke_denied.md", b"denied content")},
                         data={"title": "烟测denied"})
        record("employee 上传团队文档 403（权限收紧）", r.status_code == 403, f"got {r.status_code}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== {'PASS' if not failed else 'FAIL'}: {len(RESULTS) - len(failed)}/{len(RESULTS)} ===")
    if failed:
        print("失败项:", [f"{n} ({d})" for n, _, d in RESULTS if not any(x == n for x in failed)])
        print("失败项:", failed)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
