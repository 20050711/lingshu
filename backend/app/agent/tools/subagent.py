"""subagent 工具注册（支柱 1，2026-08-10；v2 角色化+后台化 2026-08-14）。

- 子代理 = 图外执行体（对齐 skill 工具"图外执行+受控桥"先例）：独立上下文/轮次预算/消息列表，
  主代理只看最终报告（≤2K）——21 轮主上下文探测类任务迁到子代理内部消化。
- write=True：v2 框架无授权卡（write 供复杂任务计划批准前写门禁）；
  子代理内部工具能力面收窄 + 沙盒隔离为边界依据。
- v2：role（generic/explore/plan/execute）+ mode（wait/background/wait_all/poll）+
  resume_id 续聊——background 真后台化，完成经中断队列通知主 agent。
- 队列 subagent（容量 8，超时 600s 整循环硬上限）。
"""
from __future__ import annotations

from app.agent.tools import ToolContext, ToolSpec, register_tool


async def _handler(args: dict, ctx: ToolContext) -> dict:
    """延迟导入执行体——避免循环导入（agent.subagent ⇄ agent.tools 初始化顺序）。"""
    from app.agent.subagent import run_subagent

    return await run_subagent(args, ctx)


register_tool(
    ToolSpec(
        name="subagent", progress_keys=("report", "reports", "status"),
        write=True,
        queue="subagent",
        display_name="子代理",
        icon="refresh",
        summary="委托隔离子代理完成复杂子任务（多轮调研/解析/批处理）",
        group="代码",
        sort_order=11,
        user_description=(
            "把需要多轮工具调用的复杂子任务（如解析大型模板、批量提取组件、多步骤调研）"
            "委托给隔离子代理完成——子代理独立工作，完成后返回结构化报告。"
        ),
        # 2026-09-19 实事故（下面"能力边界"段的由来，用户走查发现）：主 agent 把需要外部工具的任务
        # 派给子代理，而子代理工具面没有该工具 → 它只回了一段文字、0 次工具调用，却记为
        # 「子代理完成 failures=0」，主循环还据此算了一次"交付进展"。故把工具面边界写进描述。
        description=(
            "What：委托隔离子代理完成复杂子任务，返回结构化报告（≤2K）——"
            "子代理的内部工具轮次不进主上下文，只把最终报告带回。\n"
            "When：任务包含可隔离的多步骤工作（模板解析/批量提取/清洗/多源调研）且与主上下文无强耦合时；"
            "主上下文应保持精简，把探测类工作交给子代理。\n"
            "How：task 参数给**目标态**——要完成什么、产出文件放哪个子目录（work/sub_N/）、何时停止；"
            "不要给微操步骤。\n"
            "角色（role）：explore=只读调研（输出结构化调研结论）；plan=纯设计（只输出计划 JSON）；"
            "execute=执行（只读+沙盒+交付工具）；generic=通用（默认，只读+run_script）。\n"
            "模式（mode）：wait=同步等待报告（默认）；background=后台运行立即返回子代理 id，"
            "完成后系统会通知你；wait_all=汇合一批后台子代理报告；poll=查询状态。\n"
            "续聊：resume_id 传入已完成子代理 id 可继续使用其上下文（服务重启后失效需重派）。\n"
            "Result：wait/background 返回报告或运行状态；wait_all/poll 返回 reports 数组。\n"
            "**能力边界（派活前先看清：子代理拿不到下面这些工具，派了它也只能回段文字，"
            "白烧一轮）：**\n"
            "  ① **拿不到 MCP 外部工具**：它们走「运维 active ∧ 员工个人启用」双闸门，"
            "而子代理工具面不过这道闸门。**需要外部工具的任务必须你自己在主循环做，不要委托**。\n"
            "  ② **拿不到产出交付类**：doc_export / html_report / generate_chart / image_generation / "
            "video_generate（execute 角色也只有 doc_export/html_report/generate_chart）。"
            "**需要交付文件时，子代理只能把中间产物写进 work/sub_N/，最终由你在主循环生成**。\n"
            "  ③ **拿不到交互与广播类**：ask_user（子代理不会反问用户，缺信息要你在派活前问清）、"
            "intent_event / result_event / todo_step / memory（写）。\n"
            "  ④ tools 参数**只能收窄**（角色工具面之外的一律丢弃、不会解锁新工具），"
            "别指望用它把子代理「解锁」成能干外部工具。\n"
            "**边界：超时/失败先缩小子任务范围或拆细再派，别原样重发同一指令；子代理报告里的结论，"
            "用它之前先看是否真的完成（未完成的要如实告知用户）。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": "子任务指令（目标态：做什么/产出放哪个子目录/何时停止），如'解析模板 html 的全部组件，提取到 work/sub_1/extracted/'；wait_all/poll 模式可省略"},
                "role": {"type": "string", "enum": ["generic", "explore", "plan", "execute"],
                         "description": "可选：子代理角色（默认 generic）。explore=只读调研；plan=只输出计划 JSON；execute=可交付"},
                "mode": {"type": "string", "enum": ["wait", "background", "wait_all", "poll"],
                         "description": "可选：执行模式（默认 wait 同步等待）。background=后台运行；wait_all=汇合一批后台子代理；poll=查状态"},
                "tools": {"type": "array", "items": {"type": "string"},
                          "description": "可选：子代理可用工具白名单子集——**只能收窄**（角色工具面之外的一律丢弃，不会解锁新工具）；默认角色全集"},
                "work_subdir": {"type": "string",
                                "description": "可选：子代理 work 子目录名（默认 sub_N 自动分配）"},
                "resume_id": {"type": "string",
                              "description": "可选：续聊/查询指定子代理（已完成则保留上下文继续；运行中返回状态）"},
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "wait_all/poll 模式：目标子代理 id 列表"},
            },
            "required": [],
        },
        handler=_handler,
    )
)
