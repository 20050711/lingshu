"""doc_parse / doc_export 工具：文档解析与生成。

- doc_parse: docx/pptx/pdf → 结构化 Markdown（文本+表格+图片提取）
- doc_export: 组装 docx（python-docx，含图表 PNG 内嵌）/ pdf（soffice 转换）/
  pptx（python-pptx）/ xlsx（openpyxl）
二期已完成：图表 PNG 嵌入、pdf 导出、LibreOffice 预览（services/libreoffice.py）。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.core.url_utils import output_url


async def run_doc_parse(args: dict, ctx: ToolContext, max_chars: int | None = None) -> dict:
    file_path = str(args.get("file_path", ""))
    p = Path(file_path)
    if not p.exists():
        return {"error": f"文件不存在: {file_path}"}
    ext = p.suffix.lower()
    # 2026-08-07：max_chars 由 agent 按需指定（默认 10 万字符，上限 20 万）
    if max_chars is None:
        try:
            max_chars = min(max(int(args.get("max_chars") or 100000), 1000), 200000)
        except (TypeError, ValueError):
            max_chars = 100000
    try:
        # M9：同步解析包 to_thread（大文档解析会阻塞事件循环）
        if ext == ".docx":
            content = await asyncio.to_thread(_parse_docx, p)
        elif ext in (".pptx", ".ppt"):
            content = await asyncio.to_thread(_parse_pptx, p)
        elif ext == ".pdf":
            content = await asyncio.to_thread(_parse_pdf, p)
        else:
            return {"error": f"不支持解析的类型: {ext}（支持 docx/pptx/pdf）"}
    except Exception as e:
        return {"error": f"文档解析失败: {str(e)[:200]}"}
    return {"markdown": content[:max_chars], "file_name": p.name, "note": "以上为文档结构化解析结果。回答中展示数据请使用 Markdown 表格（| 分隔），不要用纯文本列表。"}


def _parse_docx(p: Path) -> str:
    from docx import Document

    doc = Document(str(p))
    parts = [f"# {doc.core_properties.title or p.name}"]
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
    for i, table in enumerate(doc.tables):
        parts.append(f"\n## 表格 {i + 1}")
        for row in table.rows:
            parts.append("| " + " | ".join(cell.text.strip() for cell in row.cells) + " |")
    return "\n\n".join(parts)


def _parse_pptx(p: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(p))
    parts = [f"# {p.name}"]
    for i, slide in enumerate(prs.slides):
        parts.append(f"\n## 第 {i + 1} 页")
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            elif shape.shape_type == 13:  # 图片
                parts.append(f"![图片占位: {shape.name}]")
    return "\n\n".join(parts)


def _parse_pdf(p: Path) -> str:
    import fitz  # PyMuPDF

    doc = fitz.open(str(p))
    parts = [f"# {p.name}"]
    for i, page in enumerate(doc):
        parts.append(f"\n## 第 {i + 1} 页")
        parts.append(page.get_text())
    doc.close()
    return "\n\n".join(parts)


# ===== doc_export =====

_UNI_ESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape_unicode(text: str) -> str:
    """解析 LLM 输出的 \\uXXXX 字面转义（2026-08-18：LLM 习惯用 JSON 转义写特殊字符
    （\\u7678 等），docx/pptx/xlsx 落盘后预览显示字面转义序列——统一还原为真实字符）。"""
    if not text:
        return text
    return _UNI_ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


async def run_doc_export(args: dict, ctx: ToolContext) -> dict:
    format_type = str(args.get("format", "docx")).lower()
    title = str(args.get("title") or "文档")
    sections = args.get("sections") or []  # [{heading, content}]
    table = args.get("table")  # {headers, rows}
    chart_ids = args.get("chart_ids") or []  # 二期：图表 PNG 内嵌 docx（PRD §6.6）
    # 2026-08-18：标题/章节/表格内容统一解析 \\uXXXX 字面转义
    title = _unescape_unicode(title)
    sections = [{**s, "heading": _unescape_unicode(str(s.get("heading") or "")),
                 "content": _unescape_unicode(str(s.get("content") or ""))} for s in sections]
    if isinstance(table, dict) and table.get("rows"):
        table = {**table, "rows": [[_unescape_unicode(str(c)) for c in row] for row in table["rows"]]}
    out_dir = Path(ctx.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex[:8]}.{format_type}"
    out_path = out_dir / fname

    if format_type in ("docx", "pdf"):
        # 图表 PNG 先渲染（异步），再线程池组装 docx（python-docx 同步库，防阻塞事件循环）
        png_paths = await _render_chart_pngs(chart_ids, ctx, out_dir)
        await asyncio.to_thread(_build_docx, out_path, title, sections, table, png_paths)
        if format_type == "pdf":
            # pdf = docx 生成后 soffice 转换（复用 docx 内容）
            docx_path = out_path.with_suffix(".docx")
            out_path.rename(docx_path)
            from app.services.libreoffice import to_pdf

            try:
                out_path = Path(await to_pdf(str(docx_path), str(out_dir)))
            except RuntimeError as e:
                docx_path.unlink(missing_ok=True)
                return {"error": str(e)}
            fname = out_path.name
            docx_path.unlink(missing_ok=True)
    elif format_type == "pptx":
        # 4.1：pptx 也支持图表 PNG 内嵌（复用 docx 同款渲染链路）
        png_paths = await _render_chart_pngs(chart_ids, ctx, out_dir)
        await asyncio.to_thread(_build_pptx, out_path, title, sections, table, png_paths)
    elif format_type == "xlsx":
        await asyncio.to_thread(_build_xlsx, out_path, title, sections, table)
    else:
        return {"error": f"不支持的格式: {format_type}（支持 docx/pptx/xlsx/pdf）"}

    return {
        "file_path": output_url(ctx.session_id, ctx.round_id, fname),
        "label": f"{title}.{fname.rsplit('.', 1)[-1]}",
        "note": "文件已生成，前端可下载",
    }


def _set_run_font(run, size: float, color, bold: bool = False, east_asia: str = "宋体", ascii_f: str = "Times New Roman") -> None:
    """统一设置 run 字体：西文 Times New Roman + 中文宋体，显式指定防默认字体/粗细不一致。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.font.bold = bold
    run.font.name = ascii_f
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = rPr.makeelement(qn("w:rFonts"), {})
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:ascii"), ascii_f)
    rFonts.set(qn("w:hAnsi"), ascii_f)
    rFonts.set(qn("w:eastAsia"), east_asia)


def _build_docx(out_path: Path, title: str, sections: list, table: dict | None, png_paths: list[str] | None = None) -> None:
    """专业报告模板（v3 2026-08-07，对标课程报告排版规范）：
    封面（28pt 大标题+主题色分隔线+日期）；正文宋体 12pt 行距 1.5；
    标题按 16/14/12pt 分级、统一不加粗（粗细一致）；表格浅色表头+斑马纹；图表居中。
    """
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    PRIMARY = RGBColor(0x1A, 0x56, 0xDB)
    DARK = RGBColor(0x1E, 0x29, 0x3B)
    GRAY = RGBColor(0x64, 0x74, 0x8B)
    LIGHT_FILL = "EFF6FF"

    doc = Document()
    # 默认正文：宋体 12pt（小四）、1.5 倍行距
    style = doc.styles["Normal"]
    style.font.size = Pt(12)
    style.font.color.rgb = DARK
    style.paragraph_format.line_spacing = 1.5

    def _cell_bg(cell, hex_color: str) -> None:
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:fill"), hex_color)
        cell._tc.get_or_add_tcPr().append(shd)

    def _para_bottom_border(p, color: str = "1A56DB", sz: str = "12") -> None:
        pPr = p._p.get_or_add_pPr()
        pBdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), sz)
        bottom.set(qn("w:color"), color)
        pBdr.append(bottom)
        pPr.append(pBdr)

    def _heading(text: str, level: int) -> None:
        """分级标题：16/14/12pt，统一不加粗（粗细一致），主题色。"""
        h = doc.add_heading("", level=level)
        run = h.add_run(text)
        _set_run_font(run, size={1: 16, 2: 14, 3: 12}.get(level, 14), color=PRIMARY, bold=False)
        if level == 1:
            _para_bottom_border(h)
        return h

    # ===== 封面页（大标题 28pt + 主题色分隔线 + 日期）=====
    for _ in range(5):
        doc.add_paragraph()
    cover = doc.add_paragraph()
    cover.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = cover.add_run(title)
    _set_run_font(run, size=28, color=PRIMARY, bold=False)
    _para_bottom_border(cover, sz="24")
    date_p = doc.add_paragraph()
    date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    from datetime import date

    d = date_p.add_run(date.today().strftime("%Y年%m月%d日"))
    _set_run_font(d, size=12, color=GRAY)
    doc.add_page_break()

    # ===== 章节（标题分级 16/14/12pt 不加粗）=====
    for sec in sections or []:
        if sec.get("heading"):
            _heading(str(sec["heading"]), level=1)
        for line in str(sec.get("content", "")).split("\n"):
            p = doc.add_paragraph(line)
            p.paragraph_format.space_after = Pt(6)
            _set_run_font(p.add_run(line.replace("**", "")), size=12, color=DARK, bold=False)

    # ===== 数据明细（浅色表头 + 斑马纹）=====
    if table and table.get("headers") and table.get("rows"):
        _heading("数据明细", level=1)
        t = doc.add_table(rows=1, cols=len(table["headers"]))
        t.style = "Table Grid"
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, h in enumerate(table["headers"]):
            cell = t.rows[0].cells[i]
            cell.text = str(h)
            _cell_bg(cell, LIGHT_FILL)
            for p in cell.paragraphs:
                for run in p.runs:
                    _set_run_font(run, size=10.5, color=PRIMARY, bold=False)
        for ridx, row in enumerate(table["rows"][:200]):
            cells = t.add_row().cells
            for i, v in enumerate(row):
                if i < len(cells):
                    cells[i].text = str(v)
                    for p in cells[i].paragraphs:
                        for run in p.runs:
                            _set_run_font(run, size=10.5, color=DARK, bold=False)
                    if ridx % 2 == 1:
                        _cell_bg(cells[i], "F8FAFC")

    # ===== 图表（居中嵌入）=====
    for png in png_paths or []:
        if Path(png).exists():
            _heading("图表", level=1)
            pic_p = doc.add_paragraph()
            pic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pic_p.add_run().add_picture(str(png), width=Inches(6))
    doc.save(str(out_path))


async def load_chart_options(chart_ids: list, ctx) -> list[dict]:
    """按 chart_ids 加载图表 option（4.1 抽取，doc_export 与 html_report 共用）。

    归属校验：chart 必须属于当前会话；先查同轮内存注册表（DB 落库在轮末，
    同轮内 DB 查不到），再查 ChartOutput 表；非法/越权 id 跳过。
    """
    if not chart_ids:
        return []
    from sqlalchemy import select

    from app.core.database import get_global_engine
    from app.models import ChartOutput

    engine = get_global_engine()
    options: list[dict] = []
    for cid in chart_ids:
        try:
            # 防御：非法图表 id（非 UUID，如 LLM 编造）跳过内嵌，
            # 否则绑定 uuid 列报 asyncpg DataError（曾导致图表静默丢失、LLM 反复重试）
            uuid.UUID(str(cid))
        except (ValueError, TypeError):
            from app.core.logging import get_logger

            get_logger("tools.doc_export").warning("图表 id 非法，跳过内嵌: %s", cid)
            continue
        option = None
        if getattr(ctx, "chart_registry", None):
            item = ctx.chart_registry.get(str(cid))
            if item and item.get("session_id") == ctx.session_id:
                option = item["option"]
        if option is None:
            try:
                async with engine.connect() as conn:
                    row = (await conn.execute(select(ChartOutput).where(ChartOutput.id == cid))).first()
                if row is None or row.session_id != ctx.session_id:
                    continue
                option = row.option_json
            except Exception as e:
                from app.core.logging import get_logger

                get_logger("tools.doc_export").warning("图表 %s 查询失败: %s", cid, str(e)[:120])
                continue
        options.append({"chart_id": str(cid), "option": option})
    return options


async def _render_chart_pngs(chart_ids: list, ctx, out_dir: Path) -> list[str]:
    """按 chart_ids 渲染 PNG（归属校验：chart 必须属于当前会话）。失败条目跳过。

    E-09（数据层，2026-08-10）：优先复用 ChartOutput.png_path（PNG 导出接口已落库缓存）——
    原实现每次起 Chrome 截图（Semaphore(2) 排队），已渲染 PNG 被浪费；复用失败再重渲染。
    """
    from pathlib import Path

    from sqlalchemy import text

    from app.core.database import get_global_engine

    png_paths: list[str] = []
    for item in await load_chart_options(chart_ids, ctx):
        try:
            reused = False
            async with get_global_engine().connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT png_path FROM chart_outputs WHERE id=:id AND session_id=:sid"),
                        {"id": item["chart_id"], "sid": ctx.session_id},
                    )
                ).first()
            if row and row[0]:
                p = Path(row[0])
                if p.exists() and p.is_file():
                    png_paths.append(str(p))
                    reused = True
            if not reused:
                from app.services.chart_png import render_option

                png_paths.append(await render_option(item["option"], str(out_dir)))
        except Exception as e:
            from app.core.logging import get_logger

            get_logger("tools.doc_export").warning("图表 %s 渲染失败: %s", item["chart_id"], str(e)[:120])
    return png_paths


def _build_pptx(out_path: Path, title: str, sections: list, table: dict | None, png_paths: list[str] | None = None) -> None:
    """4.1 美化：封面页（大标题 + 主题色带）+ 内容页（标题栏配色 + 页码）+ 可选图表内嵌页。"""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    PRIMARY = RGBColor(0x1A, 0x56, 0xDB)
    DARK = RGBColor(0x1E, 0x29, 0x3B)
    GRAY = RGBColor(0x64, 0x74, 0x8B)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)

    prs = Presentation()

    def _footer(slide, page_no: int) -> None:
        tb = slide.shapes.add_textbox(Inches(0.5), Inches(7.0), Inches(9.0), Inches(0.4))
        tf = tb.text_frame
        tf.paragraphs[0].text = str(page_no)
        tf.paragraphs[0].font.size = Pt(10)
        tf.paragraphs[0].font.color.rgb = GRAY
        tf.paragraphs[0].alignment = PP_ALIGN.RIGHT

    # 封面（空白布局 + 顶部主题色带 + 深色分隔细带 + 大标题 + 日期）
    from datetime import date

    cover = prs.slides.add_slide(prs.slide_layouts[6])
    band = cover.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(2.2))
    band.fill.solid()
    band.fill.fore_color.rgb = PRIMARY
    band.line.fill.background()
    strip = cover.shapes.add_shape(1, Inches(0), Inches(2.2), prs.slide_width, Inches(0.12))
    strip.fill.solid()
    strip.fill.fore_color.rgb = DARK
    strip.line.fill.background()
    tb = cover.shapes.add_textbox(Inches(0.8), Inches(2.8), Inches(11.4), Inches(1.2))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.text = title
    tf.paragraphs[0].font.size = Pt(34)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = DARK
    tf.paragraphs[0].alignment = PP_ALIGN.CENTER
    d_tb = cover.shapes.add_textbox(Inches(0.8), Inches(4.2), Inches(11.4), Inches(0.5))
    d_tf = d_tb.text_frame
    d_tf.text = date.today().strftime("%Y年%m月%d日")
    d_tf.paragraphs[0].font.size = Pt(12)
    d_tf.paragraphs[0].font.color.rgb = GRAY
    d_tf.paragraphs[0].alignment = PP_ALIGN.CENTER

    page_no = 1
    for sec in sections or []:
        page_no += 1
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = sec.get("heading", "")
        for run in s.shapes.title.text_frame.paragraphs[0].runs:
            run.font.color.rgb = PRIMARY
            run.font.bold = True
        content = str(sec.get("content", ""))
        lines = content.split("\n")[:8]
        body = s.placeholders[1]
        tf = body.text_frame
        tf.text = lines[0] if lines else ""
        for line in lines[1:]:
            tf.add_paragraph().text = line
        for para in tf.paragraphs:
            para.font.size = Pt(14)
            para.font.color.rgb = DARK
        _footer(s, page_no)

    if table and table.get("headers") and table.get("rows"):
        page_no += 1
        s = prs.slides.add_slide(prs.slide_layouts[5])  # 仅标题布局
        s.shapes.title.text = "数据明细"
        ncols = len(table["headers"])
        rows = table["rows"][:30]
        gt = s.shapes.add_table(len(rows) + 1, ncols, Inches(0.6), Inches(1.4), Inches(12.0), Inches(0.4)).table
        for j, h in enumerate(table["headers"]):
            cell = gt.cell(0, j)
            cell.text = str(h)
            cell.fill.solid()
            cell.fill.fore_color.rgb = PRIMARY
            for p in cell.text_frame.paragraphs:
                p.font.size = Pt(11)
                p.font.bold = True
                p.font.color.rgb = WHITE
        for i, row in enumerate(rows, start=1):
            for j in range(ncols):
                v = row[j] if j < len(row) else ""
                cell = gt.cell(i, j)
                cell.text = str(v)
                for p in cell.text_frame.paragraphs:
                    p.font.size = Pt(10)
        _footer(s, page_no)

    # 4.1：图表 PNG 内嵌页（每图一页，居中）
    for png in png_paths or []:
        if not Path(png).exists():
            continue
        page_no += 1
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.shapes.add_picture(str(png), Inches(1.2), Inches(1.4), width=Inches(10.9))
        _footer(s, page_no)

    prs.save(str(out_path))


def _build_xlsx(out_path: Path, title: str, sections: list, table: dict | None) -> None:
    """4.1 美化：表头主题色填充 + 自动列宽 + 冻结首行 + 数值保持原生类型。"""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title[:30] or "Sheet1"
    if table and table.get("headers"):
        headers = table["headers"]
        ws.append(headers)
        header_fill = PatternFill("solid", fgColor="1A56DB")
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        rows = (table.get("rows") or [])[:1000]
        for row in rows:
            ws.append([v if isinstance(v, (int, float)) else ("" if v is None else str(v)) for v in row])
        # 自动列宽（按内容最大长度，上限 40）
        for col_idx in range(1, len(headers) + 1):
            max_len = len(str(headers[col_idx - 1]))
            for r in rows:
                if col_idx - 1 < len(r) and r[col_idx - 1] is not None:
                    max_len = max(max_len, len(str(r[col_idx - 1])))
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 4, 40)
        ws.freeze_panes = "A2"
    wb.save(str(out_path))


register_tool(
    ToolSpec(
        name="doc_export", progress_keys=("file_path",),
        write=True,
        display_name="文档产出",
        icon="note",
        summary="生成 Word、Excel、PPT、PDF 报告文档（统一主题样式）",
        group="产出",
        sort_order=6,
        user_description=(
            "生成报告文档：Word（标题/章节/表格/图表内嵌）、Excel（主题色表头/自动列宽/冻结首行）、"
            "PPT（封面+章节+数据表+图表页）、PDF（Word 转换）。产出物可直接下载并在浏览区预览。"
        ),
        description=(
            "What：生成报告文档——docx（标题居中主题色/章节/表格/图表 PNG 内嵌）、pdf（docx 转 PDF）、"
            "pptx（封面+章节+数据表+图表页）、xlsx（主题色表头/自动列宽/冻结首行）。\n"
            "When：用户要报告/总结/周报/表格/演示文稿文件时调用。\n"
            "How：format 必填（docx/pptx/xlsx/pdf）；title 标题；sections 章节 [{heading, content}]；"
            "table 数据表 {headers, rows}；chart_ids 内嵌已有图表（同轮用 chart_id，勿编造）。\n"
            "Result：返回可下载文件（浏览区预览）。向用户说明文档结构与要点，不复述生成过程。\n"
            "**边界：生成失败先分清是哪类——缺数据（先去取数）、格式/模板不符（换模板或改用 html_report）、"
            "路径/参数问题（按提示改）；同一失败不要原样重试。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["docx", "pptx", "xlsx", "pdf"]},
                "title": {"type": "string"},
                "sections": {"type": "array", "description": "章节列表：[{heading, content}]"},
                "table": {"type": "object", "description": "可选数据表：{headers, rows}"},
                "chart_ids": {"type": "array", "description": "可选：内嵌图表的 chart_outputs.id 列表（用户要求报告中带图时提供）"},
            },
            "required": ["format", "title"],
        },
        queue="report",
        handler=run_doc_export,
    )
)
