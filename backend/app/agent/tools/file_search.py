"""统一文件检索工具 file_search（知识库版）。

按 **内容块 / 文档标题** 两层找东西：
- 内容块：知识库走 kb_chunks——「LLM 提关键词 → DB ILIKE 权重预筛 → LLM 精排」管线（services/doc_index）
- 文档标题：覆盖图片、psd、字体、视频等**永远没有文本块**的资源——按标题/文件名匹配
- 省略 query → 列清单模式（知识库文档地图）

安全：库级开关服从团队定制工具开关（团队关掉 kb 后本工具直接拒绝，HTTP 入口关了 agent 侧不留后门）；
行级权限每次调用现查（知识库三态 WHERE：全局 / 团队 / 个人），不信任任何缓存快照。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.api.tools_guard import is_custom_tool_enabled
from app.core.database import get_global_engine
from app.core.file_utils import sanitize_err_text
from app.core.logging import get_logger
from app.services.doc_index import expand_synonyms, extract_keywords, rank_candidates

logger = get_logger("tools.file_search")

_CAND_LIMIT = 30          # 内容候选块上限（喂 LLM 精排）
_ALL_TYPES = ("content", "title")   # 两层结果；types 参数可收窄（默认全给）
_TITLE_LIMIT = 10         # 文档标题命中上限
_INVENTORY_LIMIT = 50     # 列清单模式的文档条数上限
_CONTENT_BUDGET = 8000    # content 返回总字符预算（防炸上下文）
_CACHE_TTL_SEC = 300      # 检索缓存（知识库增删频繁；失效点已全覆盖，300s 作兜底）


def _safe_int(args: dict, key: str, default: int) -> int:
    try:
        return int(args.get(key) or default)
    except (TypeError, ValueError):
        return default


async def _content_matches(ctx: ToolContext, kws: list[str], top_k: int, cfg) -> tuple[list[dict], int]:
    """内容块检索：知识库，按三态权限过滤。"""
    cands: list[dict] = []
    engine = get_global_engine()
    like = [f"%{k}%" for k in kws]
    async with engine.connect() as conn:
        rows = (await conn.execute(
            text("""
                SELECT d.id AS did, d.title, c.seq, c.block_title, c.content, c.para_loc,
                       c.char_start, d.updated_at, d.category_id, d.file_path, cat.name AS category,
                       d.summary,
                       CASE WHEN d.user_id IS NOT NULL THEN 'personal'
                            WHEN d.department_id IS NOT NULL THEN 'dept' ELSE 'global' END AS source
                FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id
                LEFT JOIN kb_categories cat ON cat.id = d.category_id
                WHERE d.status = 'active'
                  AND ((d.department_id IS NULL AND d.user_id IS NULL)
                       OR d.department_id = :dept OR d.user_id = :uid)
                  AND (c.block_title ILIKE ANY(CAST(:kws AS text[]))
                       OR c.content ILIKE ANY(CAST(:kws AS text[]))
                       OR d.title ILIKE ANY(CAST(:kws AS text[])))
                ORDER BY
                  (CASE WHEN c.block_title ILIKE ANY(CAST(:kws AS text[])) THEN 3
                        WHEN c.content ILIKE ANY(CAST(:kws AS text[])) THEN 2 ELSE 1 END) DESC,
                  d.updated_at DESC
                LIMIT :cand
            """), {"dept": ctx.department_id, "uid": ctx.user_id or 0, "kws": like,
                   "cand": _CAND_LIMIT})).fetchall()
        for r in rows:
            cands.append({
                "source": "kb", "_sort_key": r.updated_at,
                "document_id": r.did, "title": r.title, "kb_source": r.source,
                "file_path": r.file_path, "category": r.category,
                "summary": (r.summary or "")[:200] or None,
                "seq": r.seq, "block_title": r.block_title or "", "content": r.content or "",
                "para_loc": r.para_loc or "", "char_start": r.char_start or 0,
            })
    if not cands:
        return [], 0
    total = len(cands)
    picked = await rank_candidates(
        kws[0] if kws else "", cands, top_k, cfg,
        lambda x: (f"《{x.get('title')}》（{x.get('block_title') or '无标题'}）: {x['content'][:300]}"),
    )
    return picked, total


async def _title_matches(ctx: ToolContext, kws: list[str]) -> list[dict]:
    """文档标题命中（资源类文件/无正文文档按标题匹配）。"""
    engine = get_global_engine()
    like = [f"%{k}%" for k in kws]
    async with engine.connect() as conn:
        rows = (await conn.execute(
            text("""
                SELECT d.id, d.title, d.file_path, d.file_type, d.summary, d.updated_at,
                       CASE WHEN d.user_id IS NOT NULL THEN 'personal'
                            WHEN d.department_id IS NOT NULL THEN 'dept' ELSE 'global' END AS source
                FROM kb_documents d
                WHERE d.status = 'active'
                  AND ((d.department_id IS NULL AND d.user_id IS NULL)
                       OR d.department_id = :dept OR d.user_id = :uid)
                  AND d.title ILIKE ANY(CAST(:kws AS text[]))
                ORDER BY d.updated_at DESC LIMIT :n
            """), {"dept": ctx.department_id, "uid": ctx.user_id or 0,
                   "kws": like, "n": _TITLE_LIMIT})).fetchall()
        return [{
            "source": "kb", "matched_by": "title",
            "document_id": d.id, "title": d.title or "（无标题）", "file_path": d.file_path,
            "file_type": d.file_type, "summary": (d.summary or "")[:200] or None,
            "kb_source": d.source,
        } for d in rows]


async def _inventory(ctx: ToolContext) -> dict:
    """列清单模式（省略 query）：返回可见范围内的知识库文档地图。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(
            text("""
                SELECT d.id, d.title, d.updated_at, d.file_path,
                       (SELECT COUNT(*) FROM kb_chunks kc WHERE kc.document_id = d.id) AS blocks,
                       CASE WHEN d.user_id IS NOT NULL THEN 'personal'
                            WHEN d.department_id IS NOT NULL THEN 'dept' ELSE 'global' END AS source
                FROM kb_documents d
                WHERE d.status = 'active'
                  AND ((d.department_id IS NULL AND d.user_id IS NULL)
                       OR d.department_id = :dept OR d.user_id = :uid)
                ORDER BY d.updated_at DESC LIMIT :n
            """), {"dept": ctx.department_id, "uid": ctx.user_id or 0,
                   "n": _INVENTORY_LIMIT})).fetchall()
        docs = [{"source": "kb", "document_id": d.id, "title": d.title or "（无标题）",
                 "blocks": d.blocks, "kb_source": d.source, "file_path": d.file_path}
                for d in rows]
    return {
        "mode": "inventory",
        "documents": docs,
        "hint": "这是知识库文档地图（不是内容检索）。确定要搜什么后带 query 再调用。",
    }


async def run_file_search(args: dict, ctx: ToolContext) -> dict:
    query = str(args.get("query") or "").strip()
    top_k = min(max(_safe_int(args, "top_k", 5), 1), 10)
    # 返回类型收窄：默认两层全给；只要文档标题清单时传 ["title"]，只要内容块传 ["content"]
    raw_types = args.get("types")
    if isinstance(raw_types, str):
        raw_types = [t.strip() for t in raw_types.split(",") if t.strip()]
    types = [t for t in (raw_types or _ALL_TYPES) if t in _ALL_TYPES] or list(_ALL_TYPES)

    # 库级开关（团队定制工具黑名单）——HTTP 入口关了，agent 侧不能留后门
    if not ctx.department_id or not await is_custom_tool_enabled(ctx.department_id, "kb"):
        return {"error": "本团队未开放知识库检索"}

    ck = (f"file_search:{ctx.department_id}:p{ctx.user_id or 0}:"
          f"{hashlib.md5(f'{query}|{top_k}|{types}'.encode()).hexdigest()}")
    try:
        from app.core.redis import redis_get

        cached = await redis_get(ck)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    try:
        if not query:
            result = await _inventory(ctx)
        else:
            from app.services.config_service import get_aux_model_cfg_for_task

            cfg = await get_aux_model_cfg_for_task(ctx.department_id, ctx.user_role, "kb_rank")
            kws = expand_synonyms(await extract_keywords(query, cfg))
            picked: list[dict] = []
            total = 0
            if "content" in types:
                picked, total = await _content_matches(ctx, kws, top_k, cfg)
            # **同一文档只留最相关的那一块**，其余折叠成 also_blocks 计数——
            # 否则一个 150+ 块的文件会把 top_k 全占满，agent 看到 5 条近似内容却拿不到别的文档。
            seen_docs: dict[int, dict] = {}
            deduped: list[dict] = []
            for m in picked:
                key = m.get("document_id")
                if key in seen_docs:
                    seen_docs[key]["also_blocks"] = seen_docs[key].get("also_blocks", 0) + 1
                    continue
                seen_docs[key] = m
                deduped.append(m)
            picked = deduped
            # content 预算
            budget = _CONTENT_BUDGET
            for m in picked:
                c_len = len(m["content"])
                if c_len > budget:
                    m["content"] = m["content"][:max(budget, 0)]
                    m["content_truncated"] = True
                budget -= min(c_len, budget)
            titles: list[dict] = []
            if "title" in types:
                titles = await _title_matches(ctx, kws)
            result = {
                "mode": "search",
                "types": types,
                "content_matches": [{k: v for k, v in m.items() if not k.startswith("_")} for m in picked],
                "title_matches": titles,
                "scanned": {"candidates": total},
                "hint": (
                    "读原文：用 file_parse(file_path, offset=char_start, length=2000) 续读"
                    "（offset 省略则从头读；要更多内容继续调，或加大 length）。"
                    "老 Office（doc/ppt/xls/wps）不支持解析——如实在答复里说明。"
                ),
            }
    except Exception as e:
        logger.warning("file_search 失败: %s", sanitize_err_text(str(e))[:200])
        return {"error": f"检索失败: {sanitize_err_text(str(e))[:150]}"}

    try:
        from app.core.redis import redis_set

        await redis_set(ck, json.dumps(result, ensure_ascii=False, default=str), _CACHE_TTL_SEC)
    except Exception:
        pass
    return result


register_tool(
    ToolSpec(
        name="file_search", progress_keys=("content_matches", "title_matches", "documents"),
        display_name="文件检索",
        icon="search",
        summary="在知识库里按内容或标题检索资料",
        group="检索",
        sort_order=4,
        user_description="在知识库里找资料：按内容找、按标题/文件名找，两类结果一次返回。",
        description=(
            "What：在知识库里检索资料：\n"
            "① 内容检索——传 query，返回命中的文本块（含所在文档与 file_path）；\n"
            "② 标题匹配——title_matches 覆盖图片/psd/字体/视频等没有正文的资源文档。\n"
            "When：用户要查任何「资料/文件/材料/记录/报表」时优先用它——不要一上来就猜文件名乱试。\n"
            "How：query 传关键词或整句（工具内部会自己提炼关键词）；top_k 默认 5。\n"
            "**types 收窄返回**（默认两层全给）：只要文档标题清单→传 [\"title\"]；"
            "只要内容块→传 [\"content\"]。\n"
            "**不传 query = 列清单**：返回可见范围的知识库文档地图（documents），"
            "适合「先看看有什么」，也适合盘点场景一次看清全貌。\n"
            "Result：content_matches（块内容 + file_path + char_start + seq + block_title）/ "
            "title_matches / documents（列清单模式）。\n"
            "读原文用 file_parse(file_path, offset=char_start, length=2000)：txt/md/csv 与 pptx/pdf/docx 的偏移"
            "精确可用；**老 Office（doc/ppt/xls/wps）与解析器回退过的文件偏移仅供近似**，"
            "file_parse 读不到该格式时如实告知用户（不要假装读过）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或整句（省略=列清单模式）"},
                "types": {
                    "type": "array", "items": {"type": "string", "enum": ["content", "title"]},
                    "description": "只返回哪些类型的结果（默认两类全给）",
                },
                "top_k": {"type": "integer", "description": "内容块返回条数（默认 5，最多 10）"},
            },
            "required": [],
        },
        queue="default",
        handler=run_file_search,
    )
)
