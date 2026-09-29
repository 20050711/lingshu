"""read_output 工具（B10）：agent 读回本会话历史轮次生成的产出文件内容。

背景：产出文件随机命名（UUID hex）+ 跨轮上下文不携带产出信息 → agent 无法"看到"自己生成的报告，
用户要求修改报告时只能凭记忆重写。本工具 + 历史【产出记录】注入解决此问题。

安全边界（B10 决策）：validate_output_readable 只放行本会话全部轮次产出根 + 本人上传根
（现 validate_readable_path 仅放行当前轮 output_dir，跨轮读不到）；kb 物理文件不放行
（file_search 走 DB 块、知识库原文件走 file_parse 白名单）；run_script 沙盒与系统路径不放行。
"""
from __future__ import annotations

from pathlib import Path

from app.agent.tools import ToolContext, ToolSpec, register_tool, validate_output_readable


async def run_read_output(args: dict, ctx: ToolContext) -> dict:
    file_path = str(args.get("file_path", "")).strip()
    if not file_path:
        return {"error": "需要提供产出文件路径（file_path，可从产出记录中获取）"}
    denied = validate_output_readable(file_path, ctx)
    if denied:
        return {"error": denied}
    # 2026-08-20（走查实锤）：产出 URL 形式（/api/v1/outputs/...）→ 磁盘路径（校验已放行，
    # 读取必须用磁盘路径；非 URL 原样）
    from app.agent.tools import resolve_output_url

    resolved = resolve_output_url(file_path, ctx) or file_path
    p = Path(resolved)
    if not p.exists() or not p.is_file():
        return {"error": f"文件不存在: {file_path}"}
    from app.agent.tools.search_tools import _slice_lines, parse_file_by_ext, parse_read_window

    # 2026-08-07：max_chars 由 agent 按需指定（默认 10 万字符，上限 20 万）
    try:
        max_chars = min(max(int(args.get("max_chars") or 100000), 1000), 200000)
    except (TypeError, ValueError):
        max_chars = 100000
    # A8（D28）：offset/length 字符窗口 + offset_line/limit_line 行号窗口（对齐 file_parse）
    offset, length = parse_read_window(args)
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
        name="read_output", progress_keys=("content", "markdown", "file_name"),
        display_name="产出读取",
        icon="file",
        summary="读取本会话此前生成的报告/表格内容（修改报告前必读）",
        group="数据",
        sort_order=6,
        user_description="用户要求修改或引用此前生成的报告时，读取该产出文件的实际内容（跨轮可读）。",
        description=(
            "What：读取本会话历史轮次生成的产出文件内容（报告/表格/文档），返回解析后的内容"
            "（xlsx 深度解析/通用转 Markdown）。\n"
            "When：用户要求修改/更新此前生成的报告（如把周报的结论改得更突出、加一列数据）、"
            "或需要引用历史产出内容时调用；产出清单见对话上下文【产出记录】。\n"
            "How：file_path 必须是**完整服务器路径**——从【产出记录】原样复制，禁止裸文件名或数字 id"
            "（本工具只放行本会话产出，知识库文件请用 file_parse）；"
            "大文件支持分片——offset/length（字符）或 offset_line/limit_line（行号）。\n"
            "Result：返回文件解析内容；修改报告时先读回原文，基于原文调整后重新调用 doc_export 生成新文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "产出文件的服务器路径（从【产出记录】中获取）"},
                "offset": {"type": "integer", "description": "可选：字符偏移起点（分段读取）"},
                "length": {"type": "integer", "description": "可选：读取字符数"},
                "offset_line": {"type": "integer", "description": "可选：按行读取起点（第 N 行，1 起）"},
                "limit_line": {"type": "integer", "description": "可选：按行读取的行数（默认 50）"},
                "max_chars": {"type": "integer", "description": "可选：单次读取字符预算（默认 100000，上限 200000）"},
            },
            "required": ["file_path"],
        },
        queue="default",
        handler=run_read_output,
    )
)
