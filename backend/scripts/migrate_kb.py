"""知识库重构迁移脚本（KB-REDESIGN，幂等，可重复执行）。

1. kb_documents 加列：file_path（物理文件路径，删文档级联删盘 M15）/ uploaded_by / status
2. kb_chunks 新表（分段存储：检索核心粒度，500-1000 字/块，预留 embedding 向量升级列）
3. 存量文档回填分段（无 chunks 的文档按现有 content 重分段，幂等）

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_kb.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_global_engine
from app.services.kb_chunker import chunk_text


async def _check_port() -> None:
    import socket

    s = socket.socket()
    try:
        s.connect(("127.0.0.1", 8000))
        print("⚠️  检测到后端正在运行（8000 端口）。请先停 uvicorn 再执行迁移！")
        sys.exit(1)
    except OSError:
        pass
    finally:
        s.close()


async def _migrate_columns(conn) -> None:
    await conn.execute(text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS file_path VARCHAR(500)"))
    await conn.execute(text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS uploaded_by INT"))
    await conn.execute(
        text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'")
    )
    print("✓ kb_documents 新增列就绪（file_path / uploaded_by / status）")


async def _create_chunks_table(conn) -> None:
    await conn.execute(
        text("""
            CREATE TABLE IF NOT EXISTS kb_chunks (
              id SERIAL PRIMARY KEY,
              document_id INT NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
              seq INT NOT NULL,
              block_title VARCHAR(255),
              content TEXT NOT NULL,
              para_loc VARCHAR(50),
              embedding REAL[],
              created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )""")
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks (document_id, seq)")
    )
    # 2026-09-10：块在原文中的字符偏移（agent 侧 file_parse(offset=char_start) 精确续读；
    # 知识库原文件对工具层开放后替代 kb_read 的"块号导航"）
    await conn.execute(text("ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS char_start INT NOT NULL DEFAULT 0"))
    print("✓ kb_chunks 表与索引就绪（含 char_start）")


async def _backfill_chunks(conn) -> int:
    """存量文档回填分段（幂等：仅处理无 chunks 的文档）。

    已知限制：存量 content 是历史 50000 字截断后的内容，回填基于截断文本（原文件路径无从找回）。
    """
    rows = (
        await conn.execute(
            text("""
                SELECT d.id, d.title, d.content FROM kb_documents d
                LEFT JOIN kb_chunks c ON c.document_id = d.id
                WHERE c.id IS NULL AND d.content IS NOT NULL
                ORDER BY d.id LIMIT 500
            """)
        )
    ).all()
    if not rows:
        print("· 存量文档均已分段（跳过回填）")
        return 0
    filled = 0
    for doc_id, title, content in rows:
        chunks = chunk_text(content or "")
        if not chunks:
            continue
        await conn.execute(
            text("""
                INSERT INTO kb_chunks (document_id, seq, block_title, content, para_loc)
                SELECT :did, x.seq, x.block_title, x.content, x.para_loc
                FROM jsonb_to_recordset(CAST(:chunks AS jsonb))
                     AS x(seq INT, block_title TEXT, content TEXT, para_loc TEXT)
            """),
            {"did": doc_id, "chunks": json.dumps(chunks)},
        )
        filled += 1
    print(f"✓ 存量回填分段：{filled} 篇（基于历史截断内容，原文件路径不可恢复）")
    return filled


async def _backfill_char_start(conn) -> int:
    """存量块的 char_start 回填（2026-09-10 新增列，旧块默认 0 → agent 会从文件头读）。

    安全策略：重新分块后**块数一致且逐块尾部 200 字吻合**才写（避开 overlap 前缀差异），
    不一致的文档整篇跳过（宁可留 0，不可写错偏移）。
    """
    from app.services.doc_index import chunk_document

    docs = (await conn.execute(text(
        "SELECT DISTINCT document_id FROM kb_chunks WHERE char_start = 0"))).fetchall()
    fixed = skipped = 0
    for (doc_id,) in docs:
        content = (await conn.execute(
            text("SELECT content FROM kb_documents WHERE id=:i"), {"i": doc_id})).scalar()
        stored = (await conn.execute(
            text("SELECT seq, content FROM kb_chunks WHERE document_id=:i ORDER BY seq"),
            {"i": doc_id})).fetchall()
        if not content or not stored:
            continue
        blocks = chunk_document(content)
        if len(blocks) != len(stored) or not blocks:
            skipped += 1
            continue
        pairs = []
        for b, s in zip(blocks, stored):
            if b["content"][-200:] != s[1][-200:]:
                pairs = []
                break
            pairs.append((b["char_start"], doc_id, s[0]))
        if not pairs:
            skipped += 1
            continue
        await conn.execute(
            text("UPDATE kb_chunks SET char_start=:c WHERE document_id=:d AND seq=:s"),
            [{"c": c, "d": d, "s": s} for c, d, s in pairs])
        fixed += 1
    print(f"✓ 存量块 char_start 回填：{fixed} 篇（跳过 {skipped} 篇——分块不一致，保守留 0）")
    return fixed


async def _reindex_docs(conn) -> int:
    """重抽取 + 重分段（`--reindex` 触发，2026-09-10 解析路径统一后的一次性修复）。

    背景：索引侧解析器由 python-docx/PyMuPDF 统一到 markitdown（= file_parse 同一路径），
    旧 content/chunks 与 agent 读到的文本不同源 → char_start 指向错误位置。
    按 file_path 重新抽取；无 file_path 的历史文档跳过（无从重取）。
    """
    from app.core.text_utils import sanitize_db_text
    from app.services.doc_index import chunk_document, extract_text

    rows = (await conn.execute(text(
        "SELECT id, file_path FROM kb_documents WHERE file_path IS NOT NULL ORDER BY id"))).fetchall()
    done = skipped = 0
    for doc_id, fp in rows:
        try:
            res = await extract_text(fp)
            if not res.ok:
                skipped += 1
                continue
            content = sanitize_db_text(res.text)
            blocks = chunk_document(content)
            if not blocks:
                skipped += 1
                continue
            for b in blocks:
                b["content"] = sanitize_db_text(b["content"])
            await conn.execute(text("DELETE FROM kb_chunks WHERE document_id=:i"), {"i": doc_id})
            await conn.execute(text("UPDATE kb_documents SET content=:c WHERE id=:i"),
                               {"c": content, "i": doc_id})
            await conn.execute(
                text("""
                    INSERT INTO kb_chunks (document_id, seq, block_title, content, para_loc, char_start)
                    SELECT :did, x.seq, x.block_title, x.content, x.para_loc, COALESCE(x.char_start, 0)
                    FROM jsonb_to_recordset(CAST(:chunks AS jsonb))
                         AS x(seq INT, block_title TEXT, content TEXT, para_loc TEXT, char_start INT)
                """), {"did": doc_id, "chunks": json.dumps(blocks)})
            done += 1
        except Exception as e:
            print(f"   ! 文档 {doc_id} 重抽取失败: {str(e)[:100]}")
            skipped += 1
    print(f"✓ 知识库重抽取：{done} 篇（跳过 {skipped} 篇）")
    return done


async def main() -> None:
    await _check_port()
    engine = get_global_engine()
    async with engine.begin() as conn:
        print("== ① kb_documents 加列 ==")
        await _migrate_columns(conn)
        print("== ② kb_chunks 建表 ==")
        await _create_chunks_table(conn)
        print("== ③ 存量回填分段 ==")
        await _backfill_chunks(conn)
        if "--reindex" in sys.argv:
            print("== ④ 全量重抽取 + 重分段（--reindex）==")
            await _reindex_docs(conn)
        else:
            print("== ④ 存量块 char_start 回填 ==")
            await _backfill_char_start(conn)
    async with engine.connect() as conn:
        n_docs = (await conn.execute(text("SELECT COUNT(*) FROM kb_documents"))).scalar()
        n_chunks = (await conn.execute(text("SELECT COUNT(*) FROM kb_chunks"))).scalar()
        print("\n== 校验 ==")
        print(f"kb_documents: {n_docs} 篇 / kb_chunks: {n_chunks} 块")
        cols = (
            await conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='kb_documents' "
                     "AND column_name IN ('file_path','uploaded_by','status') ORDER BY column_name")
            )
        ).all()
        print("kb_documents 新列:", [c[0] for c in cols])
    print("\n提示：若某团队在 system_config 配置了 dept_tools 工具白名单，需把 file_search 加进清单（新增工具）。")
    print("迁移完成 ✅（可重复执行）")


if __name__ == "__main__":
    asyncio.run(main())
