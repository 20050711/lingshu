"""web_search / xlsx_parse / file_parse 工具。

- web_search: npx websearch-deepseek（复用 DEEPSEEK_API_KEY），不可用时友好降级
- xlsx_parse: openpyxl 解析上传表格（read_only + data_only，合并单元格取左上值）
- file_parse: 按扩展名分发解析（含读取窗口 offset/length）

2026-09-10：kb_match 已下线 —— 资料检索统一由 tools/file_search.py 承担
（关键词提取与 LLM 精排管线抽到 services/doc_index.py）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from pathlib import Path

import httpx
from sqlalchemy import or_, select, text

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.text_utils import detect_binary_text, read_text_any_encoding
from app.models import KbDocument

_settings = get_settings()

_PARSE_CACHE_MAX_CHARS = 300_000   # 解析结果缓存单条上限（字符），超则不缓存防挤占 Redis


# DeepSeek 服务端原生搜索工具（web_search_20250305，Anthropic 兼容端点）
# 参考开源实现 https://github.com/lyumeng/websearch-deepseek 的 core.js：
# POST {base}/v1/messages，x-api-key 认证，服务端抓取网页并解密喂给模型生成回答。
# M4：基地址从配置派生（DEEPSEEK_BASE_URL + /anthropic），不再硬编码供应商 URL
WEBSEARCH_BASE = f"{_settings.deepseek_base_url.rstrip('/')}/anthropic"
WEBSEARCH_SYSTEM_PROMPT = (
    "You are a web search assistant. Follow these rules strictly:\n"
    "1. Use web_search to find relevant, up-to-date information for the user's query.\n"
    "2. After receiving search results, write a comprehensive, well-structured answer.\n"
    "3. Include source URLs for key claims.\n"
    "4. Do NOT call web_search again after you have results.\n"
    "5. If the user's query is not about current events, you can answer directly without searching.\n"
    "6. If search results are poor or irrelevant, explain why and suggest better keywords.\n"
    "Your response must be the final answer, not another search request."
)


async def run_web_search(args: dict, ctx: ToolContext) -> dict:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "需要提供搜索关键词"}

    # G4（全局 Explore 排查）：外部搜索 API 每次调用重复付费——同 (dept, uid, query) Redis 缓存 1h
    # （搜索结果含时效信息，1h 内复用可接受；错误不缓存——下次重试）
    import hashlib as _hl

    from app.core.redis import redis_get as _redis_get
    from app.core.redis import redis_set as _redis_set

    _wk = f"web_search:{ctx.department_id}:p{ctx.user_id or 0}:{_hl.md5(query.encode()).hexdigest()}"
    _cached = await _redis_get(_wk)
    if _cached is not None:
        try:
            return json.loads(_cached)
        except Exception:
            pass
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            resp = await c.post(
                f"{WEBSEARCH_BASE}/v1/messages",
                headers={
                    "content-type": "application/json",
                    "x-api-key": _settings.deepseek_api_key,
                },
                json={
                    "model": _settings.deepseek_model_employee,
                    "max_tokens": 8192,
                    "messages": [
                        {"role": "system", "content": WEBSEARCH_SYSTEM_PROMPT},
                        {"role": "user", "content": query},
                    ],
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "tool_choice": {"type": "auto"},
                },
            )
        if resp.status_code != 200:
            return {"error": f"联网搜索失败({resp.status_code}): {resp.text[:200]}"}
        data = resp.json()
        results: list[dict] = []
        text_parts: list[str] = []
        for block in data.get("content") or []:
            if block.get("type") == "web_search_tool_result":
                for item in block.get("content") or []:
                    results.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "content": (item.get("content") or "")[:300],
                    })
            elif block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        result = {
            "answer": "\n".join(text_parts),
            "results": results,
            "note": "以下为联网搜索结果（AI 回答+来源链接）。引用时注明来源与时效性。",
        }
        await _redis_set(_wk, json.dumps(result, ensure_ascii=False), 3600)
        return result
    except Exception as e:
        return {"error": f"联网搜索失败: {str(e)[:200]}"}


def _col_stats(headers: list[str], rows: list[list]) -> dict:
    """数值列统计（sum/avg/max/min）：放在明细前返回，即使回填被截断 LLM 也有全貌。"""
    from app.agent.tools._col_stats import col_stats

    return col_stats(headers, rows)


async def run_xlsx_parse(args: dict, ctx: ToolContext, offset: int = 0, length: int | None = None) -> dict:
    """A8（D28）：xlsx 行切片——rows[offset:offset+length]（默认 200 兼容现状），
    回传 row_start/row_end/total_rows 供 LLM 定位（"当前返回第 100-200 行/共 10000 行"）。
    """
    file_path = str(args.get("file_path", ""))
    p = Path(file_path)
    if not p.exists():
        return {"error": f"文件不存在: {file_path}"}
    # R4（红队三修复）：压缩比 100:1 + sharedStrings 100MB 炸弹预检（zip 中央目录扫描，毫秒级）；
    # 工具链此前直接 load_workbook 无预检——红队实测 100MB 炸弹解析 19.5s 物化。
    # 2026-09-17：预检随导入管线下线抽成 services/xlsx_guard.py（唯一守卫，勿删）
    from app.services.xlsx_guard import precheck_xlsx

    precheck_warn = await asyncio.to_thread(precheck_xlsx, str(p))
    if precheck_warn:
        return {"error": f"文件预检未通过: {precheck_warn}"}
    length = length or 200
    try:
        # G2（全局 Explore 排查）：xlsx 解析进程内 LRU——分片读取（offset 0-200 → 200-400 → …）
        # 每次全表重解析，10 万行表分 10 片 = 10 次全表解析。key=path|size|mtime（文件变更自动失效）
        import openpyxl

        st = p.stat()
        lru_key = f"{p}|{st.st_size}|{st.st_mtime_ns}"
        if lru_key in _XLSX_PARSE_LRU:
            _XLSX_PARSE_LRU.move_to_end(lru_key)
            parsed = _XLSX_PARSE_LRU[lru_key]
        else:
            parsed = await asyncio.to_thread(_parse_xlsx_full, p)  # 全表解析（线程内，含 wb 管理）
            _XLSX_PARSE_LRU[lru_key] = parsed
            _XLSX_PARSE_LRU.move_to_end(lru_key)
            while len(_XLSX_PARSE_LRU) > _XLSX_PARSE_LRU_MAX:
                _XLSX_PARSE_LRU.popitem(last=False)
        sheets = []
        # 2026-08-31 修复：_parse_xlsx_full 返回 {"sheets":[...]}（08-28 改结构时漏同步）——
        # 原 for sh in parsed 迭代 dict 得到键字符串 → "string indices must be integers" 必报错
        for sh in parsed["sheets"]:
            headers, all_rows = sh["headers"], sh["rows"]
            total = len(all_rows)
            seg = all_rows[offset: offset + length]
            sheets.append(
                {
                    "name": sh["name"],
                    "headers": headers,
                    "stats": _col_stats(headers, all_rows),
                    "rows": seg,
                    "row_start": offset + 1 if seg else 0,   # 1-based（表头不计）
                    "row_end": min(offset + length, total),
                    "total_rows": total,
                }
            )
        # 全表行数摘要放最前（回填截断时行数全貌不丢，LLM 无需再读文件，踩坑 26 精神）
        summary = "；".join(f"{s['name']}: {s['total_rows']} 行" for s in sheets)
        return {"sheet_summary": summary, "sheets": sheets,
                "note": "表格解析结果（sheet 内 rows 为当前窗口行，row_start/row_end/total_rows 定位用；"
                        "数据多时可传 offset/length 继续读取后续行）。"
                        "回答中展示数据请使用 Markdown 表格（| 分隔），不要用纯文本列表。"}
    except Exception as e:
        return {"error": f"表格解析失败: {str(e)[:200]}"}


# G2（全局 Explore 排查）：xlsx 全表解析进程内 LRU（分片读取防重复全表解析）
_XLSX_PARSE_LRU: "OrderedDict[str, dict]" = OrderedDict()
_XLSX_PARSE_LRU_MAX = 16


# 2026-09-17（缓存审计 P0-1）：**整文缓存**——原 file_parse 缓存键把 offset/length 也算进去，
# 于是"同一个大文件读第 2 个窗口"= 新键 = 整篇重新解析（22MB PDF 的 markitdown 是数十秒级，
# 读 5 个窗口就白解析 4 次）；且窗口结果 >30 万字符时连 Redis 都不写。
# 现改为：按文件指纹缓存**整篇解析文本**，窗口只在内存里切片（切片是纳秒级）。
# 失效靠 mtime+size 指纹（换文件即换键，不需要任何失效钩子）；大文件不写 Redis 的老规矩不变。
_FULL_TEXT_LRU: "OrderedDict[tuple, str]" = OrderedDict()
_FULL_TEXT_LRU_MAX = 4          # 单进程最多记住 4 个文件的整篇文本（内存换时间；大 PDF 一档几十 MB 级）
# excel-parser 深度解析（typed cell graph）同款：整篇结果按指纹缓存（原每次窗口重跑）
_EXCEL_DEEP_LRU: "OrderedDict[tuple, dict]" = OrderedDict()
_EXCEL_DEEP_LRU_MAX = 4


def _file_fingerprint(p: Path) -> tuple | None:
    """文件指纹 (路径, size, mtime_ns)——缓存失效唯一依据（换文件即换键）。"""
    try:
        st = p.stat()
    except OSError:
        return None
    return (str(p), st.st_size, st.st_mtime_ns)


def _lru_get(cache: "OrderedDict", key) -> object | None:
    if key is None or key not in cache:
        return None
    cache.move_to_end(key)
    return cache[key]


def _lru_put(cache: "OrderedDict", key, value, maxsize: int) -> None:
    if key is None:
        return
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > maxsize:
        cache.popitem(last=False)


def _parse_xlsx_full(p: Path) -> dict:
    """全表解析（同步，供 to_thread 包裹）：返回 {"sheets": [{"name", "headers", "rows"(全量)}]}。"""
    import openpyxl

    wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    try:
        sheets = []
        for ws in wb.worksheets:
            headers: list[str] = []
            keep: list[int] = []  # 非空表头列索引（max_col=500 会带出大量空列，展示前过滤）
            rows: list[list] = []
            # 2026-08-24（表格导出 dimension 异常）：read_only iter_rows 无参数只遍历 1 行 1 列
            # （同 import_pipeline 修复）——显式 max_row/max_col 强制全量
            for r_idx, row in enumerate(ws.iter_rows(values_only=True, max_row=1_000_000, max_col=500)):
                if row is None or all(v is None for v in row):
                    continue
                values = [str(v).strip() if v is not None else "" for v in row]
                if r_idx == 0:
                    headers = values
                    keep = [i for i, h in enumerate(headers) if h.strip()]
                    headers = [headers[i] for i in keep]
                    continue
                rows.append([values[i] for i in keep] if keep else values)
            sheets.append({"name": ws.title, "headers": headers, "rows": rows})
        return {"sheets": sheets}
    finally:
        wb.close()


register_tool(
    ToolSpec(
        name="web_search", progress_keys=("results",),
        display_name="联网搜索",
        icon="globe",
        summary="联网搜索最新资讯与公开信息",
        group="检索",
        sort_order=3,
        user_description="联网搜索互联网公开信息（新闻、行业动态、政策法规等时效性内容），返回 AI 摘要与来源链接。",
        description=(
            "What：联网搜索互联网公开信息，返回 AI 综合回答与来源链接列表。\n"
            "When：用户问最新资讯/行业动态/政策法规/时效性事实时调用；内部资料优先用 file_search（知识库）。\n"
            "How：query 搜索关键词（简洁具体，必要时可分多次搜索不同侧面）。\n"
            "Result：返回 answer（综合回答）与 results（标题/链接/摘要）；回答注明来源与时效性。\n"
            "**边界：搜索超时/服务不可用 → 是网络或服务问题，不要原样重试，如实说明或改用其他途径；"
            "没有结果就如实说没有，不要编造链接或来源。**"
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
        queue="default",
        handler=run_web_search,
    )
)


async def _parse_excel_v2(p: Path, offset: int = 0, length: int | None = None) -> dict:
    """4.1 xlsx 深度解析：excel-parser（typed cell graph + 引用 URI chunks + 公式依赖）。

    结构：sheet_summary → sheets（表头/数值统计/前 200 行，openpyxl 快路径，兼容旧结构）
    → chunks（excel-parser 深度块，含 source_uri/token/依赖摘要；统计在前防回填截断丢全貌）。
    失败回退现有 openpyxl 解析。A8：offset/length 行切片透传。
    """
    base = await run_xlsx_parse({"file_path": str(p)}, None, offset=offset, length=length)
    if "error" in base:
        return base
    # 2026-09-17（缓存审计 P0-6）：excel-parser 全量深解析也按文件指纹缓存——原每个窗口重跑一遍
    fp = _file_fingerprint(p)
    cached = _lru_get(_EXCEL_DEEP_LRU, fp)
    if cached is not None:
        base.update(cached)
        return base
    try:
        from excel_parser import parse_workbook

        result = await asyncio.to_thread(parse_workbook, str(p), mode="full")
        parts = []
        for c in list(result.chunks)[:40]:
            txt = (c.render_text or "").strip()
            if not txt:
                continue
            dep = getattr(c, "dependency_summary", None)
            dep_s = str(dep)[:120] if dep else ""
            parts.append(f"[{c.source_uri}]（token={c.token_count}，依赖:{dep_s}）\n{txt[:3000]}")
        if parts:
            extra = {
                "chunks": "\n\n".join(parts),
                "chunks_note": (
                    f"以上为 excel-parser 深度解析块（共 {len(parts)}/{result.total_chunks} 块，合计 {result.total_tokens} token），"
                    "含单元格引用与公式依赖信息，回答复杂表格问题（如'什么驱动了某指标'）时优先参考。"
                ),
            }
            base.update(extra)
            _lru_put(_EXCEL_DEEP_LRU, fp, extra, _EXCEL_DEEP_LRU_MAX)
    except Exception as e:
        base["chunks_note"] = f"深度解析不可用（{str(e)[:100]}），已返回基础表格解析。"
    return base


async def _parse_markitdown(p: Path, max_chars: int = 100000, offset: int = 0, length: int | None = None) -> dict:
    """4.1 通用文件 → Markdown（markitdown：pdf/docx/pptx/csv/html/epub/zip/ipynb/txt 等）。

    2026-08-07：max_chars 由 agent 按需指定（默认 10 万字符，上限 20 万）——1M 上下文下单次可读大文件。
    A8（D28）：offset/length 字符窗口切片（length 优先；否则 offset+max_chars 预算）。
    """
    # 2026-09-17（缓存审计 P0-1）：整篇解析结果按指纹缓存 → 分窗读取不再重复 convert
    fp = _file_fingerprint(p)
    md = _lru_get(_FULL_TEXT_LRU, fp)
    if md is None:
        try:
            from markitdown import MarkItDown

            md = await asyncio.to_thread(lambda: MarkItDown().convert(str(p)).text_content or "")
            _lru_put(_FULL_TEXT_LRU, fp, md, _FULL_TEXT_LRU_MAX)
        except Exception as e:
            from app.agent.tools.doc_tools import run_doc_parse

            # 失败回退现有 docx/pptx/pdf 解析
            return await run_doc_parse({"file_path": str(p)}, None, max_chars=max_chars)
    if length is not None:
        text = md[offset: offset + length]
    else:
        text = md[offset: offset + max_chars]
    return {
        "markdown": text,
        "file_name": p.name,
        "note": "以上为文件结构化解析结果（Markdown）。回答中展示数据请使用 Markdown 表格（| 分隔），不要用纯文本列表。",
    }


def _parse_cache_key(path: Path, max_chars: int, offset: int, length: int | None) -> str | None:
    """解析结果缓存键：`file_parse:{md5(path|mtime_ns|size|offset|length|max_chars)}`。

    2026-09-10（读者诉求「哪些缓存收益高」）：同一文件在一次会话里被 agent 读 2-3 次很常见，
    而 markitdown 解析 22MB PDF / LibreOffice 转 .doc 是数十秒级——重复解析是最大的纯浪费。
    **用 mtime+size 天然做失效**：文件被替换即换键，不需要任何失效钩子（今天刚修了 13 处
    缓存失效缺口，不再新增需要人工维护的失效点）。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    raw = f"{path}|{st.st_mtime_ns}|{st.st_size}|{offset}|{length}|{max_chars}"
    return "file_parse:" + hashlib.md5(raw.encode()).hexdigest()


async def parse_file_by_ext(
    path: Path, ctx: ToolContext, max_chars: int = 100000, offset: int = 0, length: int | None = None
) -> dict:
    """按扩展名分流解析文件内容（file_parse / read_output / 定制化工具蒸馏 共享）。

    返回结构同 file_parse：xlsx 深度解析 / 常见格式转 Markdown / docx/pptx/pdf 解析兜底。
    A8：offset/length 窗口透传。
    2026-09-10：**带结果缓存**（键含 mtime+size，见 _parse_cache_key）；只缓存成功结果
    （error 不缓存），超大结果（>300K 字符）不缓存以免挤占 Redis。
    """
    key = _parse_cache_key(path, max_chars, offset, length)
    if key:
        try:
            from app.core.redis import redis_get

            hit = await redis_get(key)
            if hit:
                return json.loads(hit)
        except Exception:
            pass
    result = await _parse_file_by_ext_uncached(path, ctx, max_chars, offset, length)
    if key and isinstance(result, dict) and not result.get("error"):
        try:
            from app.core.redis import redis_set

            payload = json.dumps(result, ensure_ascii=False)
            if len(payload) <= _PARSE_CACHE_MAX_CHARS:
                await redis_set(key, payload, _settings.file_parse_cache_ttl_s)
        except Exception:
            pass
    return result


async def _parse_file_by_ext_uncached(
    path: Path, ctx: ToolContext, max_chars: int = 100000, offset: int = 0, length: int | None = None
) -> dict:
    """实际解析（原 parse_file_by_ext 主体，勿直接调用——走上面的带缓存版本）。"""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return await _parse_excel_v2(path, offset=offset, length=length)
    if suffix in (".csv", ".txt", ".md"):
        # 2026-08-27（乱码修复）：纯文本编码检测（markitdown 对 GBK txt/csv 按 UTF-8 错位解码出乱码）；
        # 二进制内容（zip 改名 .md 等）检测后明确报错
        raw = path.read_bytes()
        if detect_binary_text(raw):
            return {"error": "文件内容为二进制/压缩数据，与文本扩展名不符（请解压出文本文件或转换格式后再解析）"}
        text = read_text_any_encoding(raw)
        if length is not None:
            text = text[offset: offset + length]
        else:
            text = text[offset: offset + max_chars]
        return {
            "markdown": text,
            "file_name": path.name,
            "note": "以上为文件文本解析结果。回答中展示数据请使用 Markdown 表格（| 分隔），不要用纯文本列表。",
        }
    if suffix in (".html", ".htm", ".epub", ".ipynb", ".zip",
                  ".pdf", ".docx", ".pptx", ".ppt", ".json", ".xml"):
        return await _parse_markitdown(path, max_chars=max_chars, offset=offset, length=length)
    from app.agent.tools.doc_tools import run_doc_parse

    return await run_doc_parse({"file_path": str(path)}, ctx, max_chars=max_chars)


def parse_read_window(args: dict) -> tuple[int, int | None]:
    """A8（D28）：读取窗口参数——字符偏移 offset/length（length 无 1000 下限，小读取不再付 1000 字符代价）。

    返回 (offset, length|None)；length=None 时调用方用 max_chars 旧预算。
    """
    try:
        offset = max(int(args.get("offset") or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    length = None
    if args.get("length") is not None:
        try:
            length = min(max(int(args["length"]), 1), 200000)
        except (TypeError, ValueError):
            length = None
    return offset, length


def _slice_lines(result: dict, line_lo: int, line_li: int) -> dict:
    """A8 行号模式：对文本类字段（markdown/chunks）按行切片，回传行区间与总行数。"""
    for key in ("markdown", "chunks"):
        if isinstance(result.get(key), str):
            lines = result[key].splitlines()
            total = len(lines)
            seg = lines[line_lo - 1: line_lo - 1 + line_li]
            result[key] = "\n".join(seg)
            note = result.get("note") or ""
            result["note"] = (note + f"\n已返回第 {line_lo}-{line_lo + len(seg) - 1} 行"
                              if seg else note + f"\n文件共 {total} 行，请求的行 {line_lo} 超出范围") \
                + f"（文件共 {total} 行）。"
            break
    return result


async def run_file_parse(args: dict, ctx: ToolContext) -> dict:
    """4.1 三层分流：xlsx/xls → excel-parser 深度解析（回退 openpyxl）；常见格式 → markitdown；
    其余 → 现有 docx/pptx/pdf 解析兜底。

    越狱防护：仅允许读取当前会话上传目录/产出目录/沙盒工作目录内的文件（防编造路径读他人文件）。
    A8（D28）：支持 offset/length（字符窗口）与 offset_line/limit_line（行号窗口）。
    """
    from pathlib import Path

    from app.agent.tools import resolve_output_url, validate_readable_path

    denied = validate_readable_path(str(args.get("file_path", "")), ctx)
    if denied:
        return {"error": denied}
    # 2026-08-07：max_chars 由 agent 按需指定（默认 10 万字符，上限 20 万）——1M 上下文单次可读大文件
    try:
        max_chars = min(max(int(args.get("max_chars") or 100000), 1000), 200000)
    except (TypeError, ValueError):
        max_chars = 100000
    # 2026-08-20（走查实锤）：产出 URL 形式（/api/v1/outputs/...）→ 磁盘路径（校验已放行，
    # 读取必须用磁盘路径；非 URL 原样）
    resolved = resolve_output_url(str(args.get("file_path", "")), ctx) or str(args.get("file_path", ""))
    p = Path(resolved)
    offset, length = parse_read_window(args)
    # 行号模式：先取全量（预算内），再按行切片（返回行区间+总行数，供 Agent 以"第 N 行"定位）
    line_lo = None
    if args.get("offset_line") is not None:
        try:
            line_lo = max(int(args["offset_line"]), 1)
        except (TypeError, ValueError):
            pass
    if line_lo is not None:
        try:
            line_li = min(max(int(args.get("limit_line") or 50), 1), 100000)
        except (TypeError, ValueError):
            line_li = 50
        result = await parse_file_by_ext(p, ctx, max_chars=200000)
        if "error" in result:
            return result
        return _slice_lines(result, line_lo, line_li)
    return await parse_file_by_ext(p, ctx, max_chars=max_chars, offset=offset, length=length)


register_tool(
    ToolSpec(
        name="file_parse", progress_keys=("sheets", "sheet_summary", "content", "markdown", "text"),
        display_name="文件解析",
        icon="file",
        summary="解析文档与表格文件（上传文件 / 知识库原文件）",
        group="数据",
        sort_order=2,
        description=(
            "What：解析文件为结构化内容——xlsx/xls 深度解析（表头/数据行/数值统计/公式依赖与单元格引用），"
            "pdf/docx/pptx/csv/html/epub/zip/ipynb 等转 Markdown。\n"
            "When：需要读取、汇总、透视、对比分析某个文件时调用。文件可能来自：本次会话上传、"
            "**file_search 返回的知识库原文件**、"
            "沙盒工作目录、历史产出。找文件先用 file_search（它直接给出 file_path）。\n"
            "How：file_path 必须是**完整服务器路径**——从 file_search 返回的 file_path、"
            "【上传文件】清单或【产出记录】中原样复制；禁止传裸文件名、数字 id 或缩写；"
            "zip 文件先 zip_extract 解压再解析。大文件支持分片读取——offset/length 按字符偏移"
            "（file_search 结果的 char_start 可直接用作 offset 续读命中块之后的原文），"
            "offset_line/limit_line 按行（返回行区间与总行数，以「第 N 行」定位）；max_chars 为字符预算（默认 100000、上限 200000）。\n"
            "Result：返回 sheet_summary/stats（数值列统计）与数据行或 Markdown 文本；"
            "xlsx 返回 row_start/row_end/total_rows 定位；回答引用真实数据，用中文表头展示，统计优先。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件的服务器路径（上传文件/沙盒工作目录文件）"},
                "offset": {"type": "integer", "description": "可选：字符偏移起点（从第 N 个字符开始读，用于分段读取）"},
                "length": {"type": "integer", "description": "可选：读取字符数（无下限；大文件分段读取用，如 offset=50000&length=20000）"},
                "offset_line": {"type": "integer", "description": "可选：按行读取起点（第 N 行，1 起）"},
                "limit_line": {"type": "integer", "description": "可选：按行读取的行数（配合 offset_line，默认 50）"},
                "max_chars": {"type": "integer", "description": "可选：单次读取字符预算（默认 100000，上限 200000；与 offset/length 二选一）"},
            },
            "required": ["file_path"],
        },
        queue="default",
        handler=run_file_parse,
    )
)
