"""知识库服务（三期 M14：两级化 全局+团队 的上传/解析/摘要；KB-REDESIGN 2026-08-07 块级重构）。

- parse_kb_document：docx/pptx/pdf/txt/md → 纯文本
- upload_kb_document：落盘 + 单事务入库（全文 + 分段 kb_chunks，物理路径入库）+ 后台摘要（失败重试）
- retry_kb_summaries：每日兜底重试（summary IS NULL 的文档，L9）
- department_id 语义：NULL=全局文档（所有团队可检索）；非 NULL=团队文档（仅本团队可见）
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.services.config_service import upload_limit_mb
from app.core.text_utils import sanitize_db_text  # 2026-08-27：清洗/解码/二进制检测提取公共版
# （2026-09-10：detect_binary_text/read_text_any_encoding 随解析器迁至 services/doc_index.py）
from app.core.database import get_global_engine
from app.core.logging import get_logger
from app.services.doc_index import (  # 2026-09-10：解析器与切块统一到通用索引层
    chunk_document,
    parse_document as parse_kb_document,
    xlsx_preview as _xlsx_preview,
)

logger = get_logger("kb.service")


async def clear_kb_match_cache() -> None:
    """知识库变更后清检索缓存（任何变更全清——宁多勿漏；kb 变更频率低成本可忽略）。

    G1（全局 Explore 排查）：原仅清 kb_match:*
    2026-09-10：**必须同时清 file_search:*** —— 知识库检索入口已由 kb_match 迁到 file_search，
    漏清会导致上传/删除文档后 10 分钟内仍返回陈旧结果（kb_smoke 实测：新建文档搜不到、
    返回的是上一轮已删除文档的缓存）。
    """
    from app.core.redis import redis_scan_delete
    for pattern in ("kb_match:*", "file_search:*"):
        try:
            await redis_scan_delete(pattern)
        except Exception as e:
            logger.warning("%s 缓存清理失败: %s", pattern, str(e)[:100])

_settings = get_settings()

# 归属三态（2026-08-24）：xlsx/csv 入知识库（.xls 旧二进制需 xlrd，暂不支持）
KB_EXTS = {".pdf", ".docx", ".pptx", ".txt", ".md", ".xlsx", ".csv"}


async def get_visible_kb_roots(user: dict) -> list[str]:
    """当前用户可见的知识库物理根清单（file_parse 路径白名单用）。

    2026-09-10（用户决策「知识库也开放原文件，统一读取路径」）：知识库物理目录
    `{upload_dir}/kb/{global|dept_id|u{user_id}}/` 的三段布局天然编码了归属三态，
    直接映射成路径前缀白名单——**与检索三态严格一致**：
      - global/ → 所有团队可读
      - {dept}/ → 本团队可读
      - u{uid}/  → 仅本人（**个人知识库对团队管理员也不可见**，勿放宽）
    安全等价性：可读文件集合与过去 kb_read（DB 行校验）完全相同，只是换了把锁
    （路径白名单用 `is_relative_to` 组件级比较）。
    """
    from app.api.tools_guard import is_custom_tool_enabled

    dept = user.get("dept_id")
    if dept and not await is_custom_tool_enabled(dept, "kb"):
        return []      # 团队停用知识库 → 物理路径也不放行（开关必须在数据层收口）
    base = Path(_settings.upload_dir) / "kb"
    roots = [str(base / "global")]
    if dept:
        roots.append(str(base / str(dept)))
    uid = user.get("user_id")
    if uid:
        roots.append(str(base / f"u{uid}"))
    return roots


async def upload_kb_document(
    filename: str,
    content: bytes,
    title: str,
    category_id: int | None,
    department_id: str | None,  # None=全局文档
    uploaded_by: int | None = None,
    user_id: int | None = None,  # 归属三态（2026-08-24）：非空=个人文档（department_id 恒 None）
) -> int:
    """上传知识文档：落盘 /data/uploads/kb/{global|dept_id}/{YYYYMM}/ → 单事务入库（全文+分段）→ 后台摘要。

    KB-REDESIGN：全文不再 50000 截断；分块存储 kb_chunks；物理文件路径入库（删文档级联删盘 M15）。
    xlsx 特例（2026-08-24）：不做 chunk_text 切分——content=预览全文，只插一条占位块
    （seq=1 block_title='表格摘要' content=预览[:2000]）——立即可检索；摘要完成后由
    _kb_summary_job 幂等替换占位块内容。
    返回 doc_id；解析失败抛 ValueError（调用方转 E003）。
    """
    from app.core.exceptions import app_error

    ext = Path(filename).suffix.lower()
    if ext not in KB_EXTS:
        raise app_error("E003", f"不支持的知识文档格式 {ext}（支持 {sorted(KB_EXTS)}）", status_code=400)
    if not content:
        raise app_error("E003", "文件为空", status_code=400)
    if len(content) > await upload_limit_mb("kb_mb") * 1024 * 1024:
        raise app_error("E003", f"文件超过 {await upload_limit_mb('kb_mb')}MB 限制", status_code=400)  # 2026-09-16 起动态

    sub = (department_id or "global") if user_id is None else f"u{user_id}"
    kb_dir = Path(f"{_settings.upload_dir}/kb/{sub}/{time.strftime('%Y%m')}")
    kb_dir.mkdir(parents=True, exist_ok=True)
    doc_path = kb_dir / f"{uuid.uuid4().hex[:12]}{ext}"
    await asyncio.to_thread(doc_path.write_bytes, content)  # 同步写盘包线程，防并发上传阻塞

    try:
        if ext == ".xlsx":
            # xlsx 特例：预览全文（表头/行数/数值统计/前 50 行），不解析为纯文本
            text_content = await asyncio.to_thread(_xlsx_preview, str(doc_path))
        else:
            text_content = await asyncio.to_thread(parse_kb_document, str(doc_path), ext)  # M9：同步解析包线程
        # 2026-08-26：入库文本清洗（部署机走查上传 500 实锤：PG UTF8 拒绝 \x00）——
        # NUL 字节（PG 拒绝 0x00）+ 非法 surrogate（asyncpg 编码拒绝）统一剔除
        text_content = sanitize_db_text(text_content)
        title = sanitize_db_text(title or "")
    except Exception as e:
        doc_path.unlink(missing_ok=True)
        # R6（红队三修复）：解析器异常文本含服务端绝对路径（红队实测回显 /data/uploads/kb/...）——脱敏
        from app.core.file_utils import sanitize_err_text

        raise app_error("E003", f"文档解析失败: {sanitize_err_text(str(e))}", status_code=400)

    # KB-REDESIGN：全文分块（500-1000 字/块，块间重叠防切断语义；同步分块包线程）
    # 2026-09-10：统一走 doc_index.chunk_document——先做格式嗅探，聊天记录类文档走消息锚点
    # 策略（日期/整条消息为边界），其余走原通用段落策略（行为与旧 chunk_text 一致）
    # xlsx 特例：不切分——只插一条占位块（立即可检索，摘要完成后替换内容）
    if ext == ".xlsx":
        chunks = [{"seq": 1, "block_title": "表格摘要", "content": text_content[:2000], "para_loc": None}]
    else:
        chunks = await asyncio.to_thread(chunk_document, text_content)

    engine = get_global_engine()
    try:
        async with engine.begin() as conn:
            doc_id = (
                await conn.execute(
                    text("INSERT INTO kb_documents (title, content, category_id, file_type, file_size, "
                         "department_id, file_path, uploaded_by, user_id, status) "
                         "VALUES (:t, :c, :cid, :ft, :fs, :dept, :fp, :ub, :uid, 'active') RETURNING id"),
                    {"t": title.strip() or filename, "c": text_content, "cid": category_id,
                     "ft": ext.lstrip("."), "fs": len(content), "dept": department_id,
                     "fp": str(doc_path), "ub": uploaded_by, "uid": user_id},
                )
            ).scalar()
            if chunks:
                # text() 原生 SQL 写 JSONB 参数须 json.dumps（踩坑 12/28）
                await conn.execute(
                    text("""
                        INSERT INTO kb_chunks (document_id, seq, block_title, content, para_loc, char_start)
                        SELECT :did, x.seq, x.block_title, x.content, x.para_loc, COALESCE(x.char_start, 0)
                        FROM jsonb_to_recordset(CAST(:chunks AS jsonb))
                             AS x(seq INT, block_title TEXT, content TEXT, para_loc TEXT, char_start INT)
                    """),
                    {"did": doc_id, "chunks": json.dumps(chunks)},
                )
    except Exception:
        # 入库失败 → 清理已落盘物理文件（复用解析失败清理模式），不留孤儿文件
        doc_path.unlink(missing_ok=True)
        raise

    # 三期 M19：celery_enabled=True 时摘要走 Celery worker，否则进程内后台任务
    if get_settings().celery_enabled:
        from app.tasks import task_kb_summary

        task_kb_summary.delay(doc_id, text_content[:20000])
    else:
        asyncio.get_event_loop().create_task(_kb_summary_job(doc_id, text_content[:20000]))
    await clear_kb_match_cache()  # G1：上传后清 kb_match 缓存（内容/清单已变）
    return doc_id


async def _kb_summary_job(doc_id: int, content: str) -> None:
    """后台生成知识文档摘要（DeepSeek，300 字内；失败 3 次指数退避重试，最终置 NULL 由每日兜底 job 补齐）。

    L9：摘要失败不影响检索（原文块直接可命中），summary 仅浏览页展示。
    模型：LLM 辅助任务档（llm_aux，按上传人团队/角色分层）→ env employee 兜底。
    """
    from openai import AsyncOpenAI

    from app.services.config_service import get_aux_model_cfg_for_user

    # 按上传人解析辅助模型（任务=知识库摘要；上传时已落 uploaded_by；旧数据为 NULL → 回退 env）
    engine0 = get_global_engine()
    async with engine0.connect() as conn:
        uploaded_by = (
            await conn.execute(text("SELECT uploaded_by FROM kb_documents WHERE id=:id"), {"id": doc_id})
        ).first()
    cfg = None
    if uploaded_by and uploaded_by[0]:
        cfg = await get_aux_model_cfg_for_user(uploaded_by[0], "kb_summary")
    # 问题 11：平台感知 client（cfg 完整传入分流——不再写死 deepseek base_url）
    from app.agent.llm_client import create_text_client

    client, model, thinking_kwargs = create_text_client(cfg)
    summary = None
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=model,
                **thinking_kwargs,
                messages=[
                    {"role": "system", "content": "为下面的知识文档生成 300 字以内的摘要，一句话概括核心内容，直接输出摘要文本。"},
                    {"role": "user", "content": content[:20000]},
                ],
            )
            summary = (resp.choices[0].message.content or "").strip()[:500]
            break
        except Exception:
            if attempt < 2:
                await asyncio.sleep(3**attempt)  # 指数退避 1s/3s
    engine = get_global_engine()
    async with engine.begin() as conn:
        # AND summary IS NULL：防兜底 job 并行已补齐时覆盖
        await conn.execute(
            text("UPDATE kb_documents SET summary=:s WHERE id=:id AND summary IS NULL"),
            {"s": summary or None, "id": doc_id},
        )
        # xlsx 特例（2026-08-24）：摘要成功后同事务幂等替换占位块 content——
        # rowcount=0 则 INSERT 兜底（占位块可能已被清理）；失败时占位块仍在（L9：摘要失败不影响检索）
        if summary:
            is_xlsx = (
                await conn.execute(text("SELECT file_type FROM kb_documents WHERE id=:id"), {"id": doc_id})
            ).first()
            if is_xlsx and is_xlsx[0] == "xlsx":
                r = await conn.execute(
                    text("UPDATE kb_chunks SET content=:s WHERE document_id=:id AND seq=1 AND block_title='表格摘要'"),
                    {"s": summary, "id": doc_id},
                )
                if r.rowcount == 0:
                    await conn.execute(
                        text("INSERT INTO kb_chunks (document_id, seq, block_title, content) VALUES (:id, 1, '表格摘要', :s)"),
                        {"id": doc_id, "s": summary},
                    )
    await clear_kb_match_cache()  # G1：摘要完成可能替换占位块 content → 清 kb_match 缓存


async def copy_kb_xlsx_to_session(user_id: int, dept_id: str, session_id: str) -> dict:
    """知识库表格文件会话复制（归属三态 2026-08-24）：把可见范围（全局/本团队/本人）内
    active 的 xlsx 物理文件复制到 {upload_dir}/users/{user_id}/{session_id}/kb_xlsx/{uuid8}_{原名}。

    - 在 upload_root 内 → file_parse 的 validate_readable_path 天然放行；
    - 不写 chat_files 表 → 不占 50 文件上限；随会话删除自动回收；
    - 单文件复制失败软跳过（不阻断）；cap=kb_xlsx_copy_max（0=关闭），达到上限返回 truncated 标记；
    - 降级文档化：会话内新传 xlsx 不追补、断线重连不重注入。
    返回 {"files": [{"file_name","file_path"}], "truncated": bool}。
    """
    cap = _settings.kb_xlsx_copy_max
    if cap <= 0:
        return {"files": [], "truncated": False}
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT title, file_path FROM kb_documents "
                     "WHERE file_type='xlsx' AND status='active' "
                     "AND ((department_id IS NULL AND user_id IS NULL) OR department_id=:dept OR user_id=:uid) "
                     "ORDER BY updated_at DESC LIMIT :lim"),
                {"dept": dept_id, "uid": user_id, "lim": cap},
            )
        ).all()
    if not rows:
        return {"files": [], "truncated": False}
    from app.core.file_utils import sanitize_filename

    dest_dir = Path(f"{_settings.upload_dir}/users/{user_id}/{session_id}/kb_xlsx")
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[dict] = []
    for title, fp in rows:
        src = Path(fp)
        if not src.is_file():
            continue  # 软失败跳过（源已被删/迁移）
        safe = sanitize_filename(title or Path(fp).name)
        safe = safe.encode("utf-8")[:240].decode("utf-8", errors="ignore").strip() or "file"
        if not safe.lower().endswith(".xlsx"):
            safe += ".xlsx"
        dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
        try:
            await asyncio.to_thread(shutil.copy2, str(src), str(dest))
        except OSError:
            continue  # 软失败跳过
        copied.append({"file_name": safe, "file_path": str(dest)})
    return {"files": copied, "truncated": len(rows) >= cap}


async def retry_kb_summaries(limit: int = 20) -> int:
    """每日兜底（02:30）：重试 7 天内 summary 为 NULL 的文档摘要（L9）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, content FROM kb_documents "
                     "WHERE summary IS NULL AND created_at > NOW() - INTERVAL '7 days' LIMIT :lim"),
                {"lim": limit},
            )
        ).all()
    for doc_id, content in rows:
        await _kb_summary_job(doc_id, (content or "")[:20000])
    return len(rows)
