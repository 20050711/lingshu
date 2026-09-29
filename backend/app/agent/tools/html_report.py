"""html_report 工具（4.1）：生成单文件 HTML 数据报告。

- jinja2 模板（backend/app/static/templates/report.html.j2），**内联 echarts.min.js**（iframe blob 预览离线可用）
- chart_ids 对应 option 注入 window.__CHARTS__，加载后逐个 echarts.init 渲染
- 样式统一深海蓝主题（对齐前端 theme.css 与 chart_builder 设计规范）
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.agent.tools.doc_tools import load_chart_options
from app.core.config import get_settings
from app.core.url_utils import output_url

_settings = get_settings()

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "templates"
_ECHARTS_JS = Path(__file__).resolve().parent.parent.parent / "static" / "echarts.min.js"

_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=select_autoescape(["html", "j2"]))


async def run_html_report(args: dict, ctx: ToolContext) -> dict:
    # 2026-08-18：\\uXXXX 字面转义解析（与 doc_export 对齐——LLM 输出特殊字符的 JSON 转义习惯）
    from app.agent.tools.doc_tools import _unescape_unicode

    title = _unescape_unicode(str(args.get("title") or "数据报告"))
    sections = [{**s, "heading": _unescape_unicode(str(s.get("heading") or "")),
                 "content": _unescape_unicode(str(s.get("content") or ""))} for s in (args.get("sections") or [])]
    table = args.get("table") or {}
    if table.get("rows"):
        table = {**table, "rows": [[_unescape_unicode(str(c)) for c in row] for row in table["rows"]]}
    chart_ids = args.get("chart_ids") or []

    # 加载图表 option（内存注册表优先，再查 DB；非法 id 跳过）
    chart_options = await load_chart_options(chart_ids, ctx)
    charts = [
        {"label": str(opt.get("label") or f"图表 {i + 1}"), "option": opt["option"]}
        for i, opt in enumerate(chart_options)
    ]

    # 内联 echarts.min.js（防 "</" 截断 script 标签）
    try:
        echarts_js = _ECHARTS_JS.read_text(encoding="utf-8").replace("</", "<\\/")
    except OSError:
        return {"error": "echarts.min.js 缺失（backend/app/static/），无法生成 HTML 报告"}

    try:
        tpl = _env.get_template("report.html.j2")
        html = tpl.render(
            title=title,
            date=date.today().strftime("%Y-%m-%d"),
            sections=sections or [],
            table={"headers": table.get("headers") or [], "rows": (table.get("rows") or [])[:200]},
            charts=charts,
            # SEC-04：charts_json 复用 echarts_js 同款防截断（json.dumps 不转义 "</script>"，
            # 数据含 "</" 即截断 script 标签 → 同源存储型 XSS；模板 {{ charts_json | safe }} 保留）
            charts_json=json.dumps([c["option"] for c in charts], ensure_ascii=False).replace("</", "<\\/"),
            echarts_js=echarts_js,
        )
    except Exception as e:
        return {"error": f"HTML 报告生成失败: {str(e)[:200]}"}

    out_dir = Path(ctx.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex[:8]}.html"
    (out_dir / fname).write_text(html, encoding="utf-8")

    return {
        "file_path": output_url(ctx.session_id, ctx.round_id, fname),
        "label": f"{title}.html",
        "note": "HTML 报告已生成（含内嵌图表），前端可预览与下载。",
    }


register_tool(
    ToolSpec(
        name="html_report", progress_keys=("file_path",),
        write=True,
        display_name="网页报告",
        icon="screen",
        summary="生成单文件 HTML 数据报告（内嵌交互图表）",
        group="产出",
        sort_order=7,
        user_description=(
            "生成单文件 HTML 数据报告：标题、章节内容、数据表格与内嵌交互图表（ECharts），"
            "浏览器直接打开即可查看，可下载保存。适用于汇报、可视化报告等需要网页呈现的场景。"
        ),
        description=(
            "What：生成单文件 HTML 数据报告（jinja2 模板 + 内联 echarts，章节/表格/交互图表，离线可预览）。\n"
            "When：用户要网页版报告/可视化页时调用；多图表联动或数据仪表盘需求转 framework 技能。\n"
            "【适用边界】本工具使用**固定深海蓝报告模板**（标题/章节/表格/内嵌图表）——"
            "**不适合复刻用户自定义模板的外观/样式/交互**；用户要求「同款模板/仿样式/自定义交互」时，"
            "用 framework 的 html 模式（可自写任意 HTML）或 run_script 提取模板组件 + mode=deliver 交付。\n"
            "How：title 标题；sections 章节 [{heading, content}]；table 数据表 {headers, rows}；"
            "chart_ids 内嵌已有图表（同轮用 chart_id，勿编造）。\n"
            "Result：返回可预览的 HTML 文件。向用户说明报告结构与要点。\n"
            "**边界：生成失败多为模板/参数问题——按错误提示调整后再调一次，别原样重发；"
            "报告内容没数据支撑时先去取数，不要用占位内容凑数。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "报告标题"},
                "sections": {"type": "array", "description": "章节列表：[{heading, content}]"},
                "table": {"type": "object", "description": "数据表：{headers, rows}"},
                "chart_ids": {"type": "array", "description": "内嵌图表的 chart_id 列表"},
            },
            "required": ["title"],
        },
        queue="report",
        handler=run_html_report,
    )
)
