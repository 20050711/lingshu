"""意图预告/结果反馈工具（v2 框架，2026-08-14）。

小事件粒度由 LLM 自定（设计书 §四）：LLM 在事件首次工具调用前调 intent_event 广播意图，
事件结束后调 result_event 广播结果（内部重试/非用户向回合不广播）。
tool_exec 对这两个工具特判：只广播 SSE 事件、不发 tool 事件、不进工具明细、
result 的 duration_s 由 tool_exec 按 intent 时间戳补算——handler 仅为占位。
"""
from __future__ import annotations

from app.agent.tools import ToolContext, ToolSpec, register_tool

_INTENT_PARAMS = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "意图预告（正式书面语，概括动作与对象，如「查询各区域销售数据」），不超过 40 字",
        },
    },
    "required": ["text"],
}

_RESULT_PARAMS = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "结果汇报（正式书面语，给出结论与关键数字，如「各区域销售查询完成：华东最高 7,792 元」），不超过 60 字",
        },
        "ok": {
            "type": "boolean",
            "description": "本事件是否成功（true/false）",
        },
    },
    "required": ["text", "ok"],
}


async def _handler(args: dict, ctx: ToolContext) -> dict:
    # tool_exec 特判拦截，不会真正派发到这里；返回占位防直调
    return {"ok": True, "note": "事件已广播"}


register_tool(ToolSpec(
    name="intent_event", progress_keys=("ok",),
    description=(
        "What：广播一条「意图预告」给用户（小事件开始前调用一次）。\n"
        "When：一个用户可理解的动作单元（小事件）即将开始、首次调用业务工具之前。\n"
        "How：text 用一句正式书面语说清本事件要做什么（用户视角，不说内部工具名，避免口语）。\n"
        "Result：前端显示灰色意图气泡；事件结束后必须调 result_event 汇报结果。\n"
        "约束：内部重试/验证性探测等非用户向回合不调用；一个事件只调一次。"
    ),
    parameters=_INTENT_PARAMS,
    queue="default",
    handler=_handler,
    display_name="意图预告",
    icon="chat",
    status="active",
    sort_order=90,
    group="系统",
    summary="广播小事件意图预告（用户可见）",
    select_mode="multi",
))

register_tool(ToolSpec(
    name="result_event", progress_keys=("ok",),
    description=(
        "What：广播一条「结果反馈」给用户（小事件结束后调用一次）。\n"
        "When：intent_event 预告的事件完成后（含失败——失败时 ok=false 并如实说明）。\n"
        "How：text 用正式书面语给出一句话结论（含关键数字），ok 标记成功/失败。\n"
        "Result：前端显示绿勾/红叉结果条（含耗时，系统自动补算）。\n"
        "约束：与 intent_event 一一对应；事件中途被放弃也要补发 ok=false。"
    ),
    parameters=_RESULT_PARAMS,
    queue="default",
    handler=_handler,
    display_name="结果反馈",
    icon="check",
    status="active",
    sort_order=91,
    group="系统",
    summary="广播小事件结果反馈（用户可见）",
    select_mode="multi",
))
