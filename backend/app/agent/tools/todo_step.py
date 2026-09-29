"""todo_step 工具（v2 复杂任务 P6，2026-08-14）：步骤清单推进。

复杂任务执行期专用：开工即建步骤清单（前端实时勾选），每步开始/结束/失败广播
scope="step" 的 intent/result 事件。tool_exec 特判处理（不发 tool 事件、不进工具明细），
handler 仅为占位。
action=replan：用户插话推翻了已批准计划 → 回 P5 重新批准（修订循环）。
"""
from __future__ import annotations

from app.agent.tools import ToolContext, ToolSpec, register_tool

_PARAMS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start", "done", "fail", "replan"],
            "description": "start=步骤开始；done=步骤完成（summary 给结果一句话）；fail=步骤失败（summary 给原因）；replan=用户插话推翻计划，回计划批准卡重新提交",
        },
        "step_id": {
            "type": "string",
            "description": "计划 JSON 中的步骤 id（如 s1、s2）；replan 可省略",
        },
        "summary": {
            "type": "string",
            "description": "done/fail 的结果一句话（含关键数字/失败原因，≤60 字）；start 可省略",
        },
    },
    "required": ["action"],
}


async def _handler(args: dict, ctx: ToolContext) -> dict:
    # tool_exec 特判拦截，不会真正派发到这里；返回占位防直调
    return {"ok": True, "note": "todo 步骤已广播"}


register_tool(ToolSpec(
    name="todo_step", progress_keys=("ok",),
    description=(
        "What：推进执行步骤清单（复杂任务执行期专用，计划批准后步骤清单已在前端展示）。\n"
        "When：每执行一个计划步骤：开始前调 start、完成后调 done、失败调 fail；"
        "用户中途插话推翻了已批准计划时调 replan（回计划批准卡修订）。\n"
        "How：step_id 必须用计划 JSON 中的步骤 id（s1/s2/...）；summary 一句话（≤60 字）。\n"
        "Result：前端步骤清单实时勾选（绿勾完成/红叉失败/当前步高亮）；用户全程可见进度。"
    ),
    parameters=_PARAMS,
    queue="default",
    handler=_handler,
    display_name="步骤清单",
    icon="check",
    status="active",
    sort_order=92,
    group="系统",
    summary="执行步骤清单推进（复杂任务）",
    select_mode="multi",
))
