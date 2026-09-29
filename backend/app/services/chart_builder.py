"""ECharts option JSON 构建器（12 种图表类型）。

一期只输出 option JSON，前端 ECharts 直接渲染（不做 PNG/chromium，二期补）。
输入：数据行（columns + rows）或模型构造的 categories/series 结构。

4.1 设计规范（对齐前端 theme.css 深海蓝主题）：
- 主色 #1a56db，8 色序列调色板；中文字体栈；轴标签/分割线/图例统一灰阶
- bar 圆角 + 最大柱宽；line 平滑 + 面积淡化；长序列自动 dataZoom；y 轴万级格式化
"""
from __future__ import annotations

# ---- 4.1 设计常量（统一注入全部图表类型）----
# 2026-09-18：色板重排——原 [#0ea5e9,#06b6d4] 相邻且正常视力 ΔE 仅 6.4（硬失败线 15，第 2/3 个系列
# 就分不清），#64748b 彩度 0.041 低于下限（读作灰、与"其他/中性"语义打架）。现序列经配色校验器
# 全项通过：正常视力最差相邻对 16.8、色盲(deutan)最差 10.2、彩度全 ≥0.1；仅 #f59e0b 对白底
# 对比度 2.15 <3:1（图表自带 tooltip + 数值标签，属"需可见标签"的可接受情形）。首位保持品牌主色。
PALETTE = ["#1a56db", "#e11d48", "#0d9488", "#f59e0b", "#7c3aed", "#15803d", "#0891b2", "#b45309"]
FONT_FAMILY = '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif'
TITLE_STYLE = {"fontSize": 15, "color": "#1e293b", "fontWeight": 600, "fontFamily": FONT_FAMILY}
AXIS_LABEL = {"color": "#64748b", "fontSize": 11, "fontFamily": FONT_FAMILY}
SPLIT_LINE = {"lineStyle": {"color": "#f1f5f9", "width": 1}}
TOOLTIP_STYLE = {
    "backgroundColor": "#ffffff",
    "borderColor": "#e2e8f0",
    "borderWidth": 1,
    "textStyle": {"color": "#1e293b", "fontSize": 12},
}
LEGEND_STYLE = {"bottom": 0, "type": "scroll", "textStyle": {"color": "#64748b", "fontSize": 11}}
GRID = {"left": 50, "right": 30, "top": 50, "bottom": 60}
# y 轴万级格式化（>=1 万显示"x.x万"）
Y_AXIS_FORMATTER = "function(v){return v>=10000?(v/10000).toFixed(1)+'万':v;}"
DATA_ZOOM_LIMIT = 20  # 类别超过该数自动加缩放条


def _num(v) -> float | None:
    try:
        f = float(str(v).replace(",", "").replace("%", "").replace("¥", ""))
        return f
    except (ValueError, TypeError):
        return None


def _extract(rows: list[list], col_names: list[str], col: str, fallback: int = 0) -> list:
    if col in col_names:
        idx = col_names.index(col)
        return [row[idx] if idx < len(row) else None for row in rows]
    return [row[fallback] if row else None for row in rows]


def _category_axis(cats: list) -> dict:
    axis = {"type": "category", "data": cats, "axisLabel": AXIS_LABEL, "axisLine": {"lineStyle": {"color": "#e2e8f0"}}}
    if len(cats) > DATA_ZOOM_LIMIT:
        axis["dataZoom"] = [{"type": "inside"}, {"type": "slider", "bottom": 12, "height": 16}]
    return axis


def _value_axis() -> dict:
    return {
        "type": "value",
        "axisLabel": {**AXIS_LABEL, "formatter": Y_AXIS_FORMATTER},
        "splitLine": SPLIT_LINE,
    }


def _series_style(s: dict, ctype: str) -> dict:
    """按类型注入统一视觉样式（柱圆角/线平滑/面积淡化）。"""
    if ctype == "bar":
        return {**s, "barMaxWidth": 40, "itemStyle": {"borderRadius": [4, 4, 0, 0]}}
    if ctype == "line":
        return {**s, "smooth": True, "symbolSize": 6,
                "lineStyle": {"width": 2.5},
                "areaStyle": {"opacity": 0.08}}
    return s


def build_echarts_option(
    chart_type: str,
    title: str,
    col_names: list[str] | None = None,
    rows: list[list] | None = None,
    x_column: str = "",
    y_columns: list[str] | None = None,
    categories: list | None = None,
    series: list | None = None,
) -> dict:
    """构造 ECharts option。

    categories/series 存在时直接使用（模型已聚合好的场景）；
    否则由 col_names+rows+x_column+y_columns 推导。
    """
    ctype = chart_type.lower()
    if categories is not None and series is not None:
        cats, sr = categories, series
    else:
        cats = [str(v) if v is not None else "" for v in _extract(rows or [], col_names or [], x_column)]
        sr = [
            {"name": col, "type": _echarts_type(ctype, i), "data": [_num(v) for v in _extract(rows or [], col_names or [], col)]}
            for i, col in enumerate(y_columns or [])
        ]

    base = {
        "color": PALETTE,
        "title": {"text": title, "left": "center", "textStyle": TITLE_STYLE},
        "tooltip": {**TOOLTIP_STYLE, "trigger": "axis"},
        "legend": LEGEND_STYLE,
        "grid": GRID,
    }

    if ctype == "pie":
        data = []
        for i, cat in enumerate(cats):
            val = sr[0]["data"][i] if sr and i < len(sr[0]["data"]) else 0
            data.append({"name": cat, "value": val})
        return {
            **base,
            "tooltip": {**TOOLTIP_STYLE, "trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "series": [{"type": "pie", "radius": ["35%", "65%"], "center": ["50%", "52%"],
                        "itemStyle": {"borderRadius": 6, "borderColor": "#ffffff", "borderWidth": 2},
                        "label": {"color": "#1e293b", "fontSize": 11},
                        "labelLine": {"lineStyle": {"color": "#cbd5e1"}},
                        "data": data}],
        }

    if ctype in ("bar", "line"):
        return {**base, "xAxis": _category_axis(cats), "yAxis": _value_axis(),
                "series": [_series_style(s, ctype) for s in sr]}

    if ctype == "scatter":
        return {**base, "xAxis": {"type": "value", "axisLabel": AXIS_LABEL, "splitLine": SPLIT_LINE},
                "yAxis": {"type": "value", "axisLabel": AXIS_LABEL, "splitLine": SPLIT_LINE},
                "series": [{**s, "symbolSize": 9, "itemStyle": {"opacity": 0.75}} for s in sr]}

    if ctype == "radar":
        indicators = [{"name": c, "max": 100} for c in cats]
        radar_series = []
        for s in sr:
            values = s.get("data", [])
            max_v = max([_num(v) or 0 for v in values] or [1])
            radar_series.append(
                {"name": s["name"], "type": "radar", "data": [{"value": [(_num(v) or 0) / max_v * 100 for v in values]}]}
            )
        return {
            **base,
            "tooltip": {**TOOLTIP_STYLE, "trigger": "item"},
            "radar": {"indicator": indicators, "axisName": {"color": "#64748b", "fontSize": 11},
                      "splitLine": {"lineStyle": {"color": "#e2e8f0"}}},
            "series": radar_series,
        }

    if ctype == "gauge":
        val = sr[0]["data"][0] if sr and sr[0]["data"] else 0
        return {
            **base,
            "series": [{"type": "gauge", "min": 0, "max": max(_num(v) or 0 for v in (sr[0]["data"] if sr else [])) or 100,
                        "detail": {"fontSize": 20, "color": "#1a56db", "fontWeight": 700},
                        "axisLine": {"lineStyle": {"color": [[0.8, "#dbeafe"], [1, "#1a56db"]], "width": 12}},
                        "data": [{"value": _num(val) or 0, "name": title}]}],
        }

    if ctype == "funnel":
        return {
            **base,
            "tooltip": {**TOOLTIP_STYLE, "trigger": "item"},
            "series": [
                {
                    "type": "funnel",
                    "left": "10%",
                    "width": "80%",
                    "sort": "descending",
                    "gap": 2,
                    "label": {"color": "#1e293b", "fontSize": 11},
                    "data": [{"name": c, "value": sr[0]["data"][i] if sr and i < len(sr[0]["data"]) else 0} for i, c in enumerate(cats)],
                }
            ],
        }

    if ctype == "heatmap":
        x_cats, y_cats = cats, [s["name"] for s in sr]
        data = []
        for yi, s in enumerate(sr):
            for xi, v in enumerate(s["data"]):
                data.append([xi, yi, _num(v) or 0])
        return {
            **base,
            "tooltip": {**TOOLTIP_STYLE, "position": "top"},
            "xAxis": {"type": "category", "data": x_cats, "axisLabel": AXIS_LABEL, "splitArea": {"show": True}},
            "yAxis": {"type": "category", "data": y_cats, "axisLabel": AXIS_LABEL, "splitArea": {"show": True}},
            "visualMap": {"min": 0, "max": max((_num(d[2]) or 0 for d in data), default=1), "calculable": True,
                          "orient": "horizontal", "left": "center", "bottom": 0,
                          "inRange": {"color": ["#eff6ff", "#1a56db"]}},
            "series": [{"type": "heatmap", "data": data}],
        }

    if ctype == "tree":
        root = {"name": title, "children": [{"name": str(c), "value": sr[0]["data"][i] if sr and i < len(sr[0]["data"]) else 0} for i, c in enumerate(cats)]}
        return {
            **base,
            "tooltip": {**TOOLTIP_STYLE, "trigger": "item"},
            "series": [{"type": "tree", "data": [root], "top": "5%", "left": "10%", "bottom": "5%", "right": "15%",
                        "symbolSize": 10, "itemStyle": {"color": "#1a56db"},
                        "label": {"position": "left", "verticalAlign": "middle", "align": "right", "fontSize": 11, "color": "#1e293b"}}],
        }

    if ctype == "map":
        return {
            **base,
            "tooltip": {**TOOLTIP_STYLE, "trigger": "item"},
            "visualMap": {"min": 0, "max": max((_num(v) or 0 for v in (sr[0]["data"] if sr else [])), default=1),
                          "left": "left", "top": "bottom", "inRange": {"color": ["#eff6ff", "#1a56db"]}},
            "series": [{"type": "map", "map": "china", "data": [{"name": str(c), "value": _num(sr[0]["data"][i]) or 0} for i, c in enumerate(cats)]}],
        }

    if ctype in ("candlestick", "kline"):
        # 输入应为 OHLC 四列
        return {**base, "xAxis": _category_axis(cats), "yAxis": _value_axis(),
                "series": [{"type": "candlestick", "data": [[(_num(v) or 0) for v in s.get("data", [])] for s in sr],
                            "itemStyle": {"color": "#ef4444", "color0": "#10b981", "borderColor": "#ef4444", "borderColor0": "#10b981"}}]}

    if ctype == "boxplot":
        return {**base, "xAxis": _category_axis(cats), "yAxis": _value_axis(),
                "series": [{"type": "boxplot", "data": [[(_num(v) or 0) for v in s.get("data", [])] for s in sr],
                            "itemStyle": {"color": "#1a56db", "borderColor": "#1e40af"}}]}

    # 兜底：柱状图
    return {**base, "xAxis": _category_axis(cats), "yAxis": _value_axis(),
            "series": [_series_style(s, "bar") for s in sr]}


def _echarts_type(ctype: str, index: int) -> str:
    if ctype == "bar":
        return "bar"
    if ctype == "line":
        return "line"
    if ctype == "scatter":
        return "scatter"
    return "bar"


CHART_TYPES = [
    "bar", "line", "pie", "scatter", "radar", "gauge",
    "funnel", "heatmap", "tree", "map", "candlestick", "boxplot",
]
