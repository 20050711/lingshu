"""chart_gen 工具：由数据构造 ECharts option JSON（12 种图表类型）。

模型流程建议：从资料文件取数（file_parse/run_script）→ 把 columns/rows 传给 chart_gen 画图。
"""
from __future__ import annotations

import uuid

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.services.chart_builder import CHART_TYPES, build_echarts_option


async def _same_chart_exists(ctx: ToolContext, title: str) -> bool:
    """同会话同名图表去重（Q10，2026-08-26）：chart_registry 同轮 + chat_messages.outputs 跨轮。

    命中 → 引导 LLM 引用已有产出，不重复生成（部署机实锤 r4 单轮 6 次同名图表）。
    """
    if ctx.chart_registry:
        for v in ctx.chart_registry.values():
            opt = v.get("option") or {}
            if opt.get("title") == title:
                return True
    if not ctx.session_id:
        return False
    from sqlalchemy import text

    from app.core.database import get_global_engine

    try:
        engine = get_global_engine()
        async with engine.connect() as conn:
            r = (
                await conn.execute(
                    text("SELECT 1 FROM chat_messages m, jsonb_array_elements(m.outputs) o "
                         "WHERE m.session_id=:s AND m.role='assistant' "
                         "AND o->>'type'='chart' AND o->>'label'=:t LIMIT 1"),
                    {"s": ctx.session_id, "t": title},
                )
            ).first()
            return r is not None
    except Exception:
        return False


async def run_chart_gen(args: dict, ctx: ToolContext) -> dict:
    chart_type = str(args.get("chart_type", "bar")).lower()
    if chart_type not in CHART_TYPES:
        return {"error": f"不支持的图表类型 {chart_type}，可选：{', '.join(CHART_TYPES)}"}

    title = str(args.get("title") or "数据图表")
    if await _same_chart_exists(ctx, title):
        return {"error": f"图表「{title}」已在本会话生成过（见上方产出记录），请直接引用该图表并说明数据结论，"
                         "不要重复生成；如确需新图请更换标题"}
    col_names = args.get("columns") or []
    rows = args.get("rows") or []

    option = build_echarts_option(
        chart_type=chart_type,
        title=title,
        col_names=col_names,
        rows=rows,
        x_column=str(args.get("x_column") or (col_names[0] if col_names else "")),
        y_columns=args.get("y_columns") or [c for c in col_names[1:]][:3],
    )

    return {
        "chart_type": chart_type,
        "chart_label": title,
        "option": option,
        "note": "图表已生成，前端渲染。可在回答中说明图表观察到的数据结论。",
    }


register_tool(
    ToolSpec(
        name="generate_chart", progress_keys=("chart_id", "chart_label", "option", "file_path"),
        write=True,
        display_name="图表生成",
        icon="chart",
        summary="生成柱状图、折线图、饼图等可视化图表",
        group="产出",
        sort_order=5,
        description=(
            "What：生成 ECharts 图表 option（12 种类型，统一深海蓝主题样式），前端实时渲染。\n"
            "When：用户要「看图/可视化」时，先取数（file_parse 读表 / run_script 汇总）再调用；同一图表只生成一次（chart_id 引用）。\n"
            "How：chart_type 按数据形状选择——时间序列→line；类别对比/排名→bar（排名用横向 bar）；占比构成→pie/funnel；"
            "相关分布→scatter；多指标对比→分组 bar；综合能力→radar；地理→map。columns/rows 直接传你拿到的数据（表头 + 数据行）。\n"
            "Result：返回 option JSON + chart_id；回答中说明图表反映的数据结论（趋势/占比/异常），不复述 option 原文。\n"
            "**边界：数据为空或与该图表类型不匹配（如文本列画折线）→ 换类型或先取数，"
            "别用同一份数据重复提交同样的图表。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": CHART_TYPES, "description": "图表类型"},
                "title": {"type": "string", "description": "图表标题"},
                "columns": {"type": "array", "items": {"type": "string"}, "description": "数据列名（来自你解析出的表格表头）"},
                "rows": {"type": "array", "description": "数据行（元素为数组，与 columns 一一对应）"},
                "x_column": {"type": "string", "description": "X 轴列名"},
                "y_columns": {"type": "array", "items": {"type": "string"}, "description": "Y 轴列名列表（最多 3 个）"},
            },
            "required": ["chart_type", "title", "columns", "rows"],
        },
        queue="report",
        handler=run_chart_gen,
    )
)
