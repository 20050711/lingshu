"""LangGraph AgentState 定义。

messages 使用原生 dict 列表（含 reasoning_content / tool_calls 字段），
手动 append、不经 LangChain 序列化——这是 DeepSeek 思考模式下多轮
工具调用能正确回传 reasoning_content（否则 400）的关键。
"""
from typing import TypedDict


class AgentState(TypedDict, total=False):
    # 会话上下文
    session_id: str
    round_id: int
    user_question: str
    user_id: int                        # 用户 ID（个人记忆注入/工具隔离）
    user_role: str                      # ceo / employee / dept_admin / admin
    department_id: str
    client_id: str

    # v2 交互框架（2026-08-14）：双模式与反问
    # 踩坑：next 是 plan_router 的路由输出键——必须声明进 TypedDict，否则 LangGraph
    # 对非 schema 键的节点返回静默丢弃，条件边永远读到缺省值（路由失效，硬查 2 小时）
    next: str                           # plan_router 路由输出（ask_question/skill_router/plan_approval/chat；消费后可缺省）
    mode: str                           # "quick" | "complex"（chat_service 注入，sessions.mode 镜像；仅许升级）
    upgrade: bool                       # 本次 ask 为快速→复杂升级（P0 衔接注入）
    question_round: int                 # 复杂任务已问反问轮数（0/1/2；quick 恒 0）
    question_id: str                    # 当前等待中的 question_id（ask_question 写入）
    asked_this_round: bool              # 本 ask 已问过（quick 1 轮上限焊死）
    interrupt_note: str                 # 本回合已 LPOP 的中断注入文本（agent_llm 消费后清空）
    intent_ts: float | None             # intent_event 时间戳（result_event 补算 duration_s）
    token_usage: dict                   # 需求 3（2026-08-17）：本轮 ask LLM usage 累计 {prompt_tokens, completion_tokens}（deepseek 平台）

    # v2 复杂任务 P0-P7 状态机（2026-08-14）
    phase: str                          # quick 恒 "exec"；complex：clarify/explore/design/tradeoff/approval/execute/final
    phase_instruction: str              # plan_router 生成的本阶段注入指令（agent_llm 追加进本轮 user 消息后清空）
    plan_json: dict | None              # 计划 JSON {goal, steps[{id,title,intent,verify}], risks[], questions_answered, plan_id?}
    plan_approval: str                  # "" / approved / rejected / timeout（plan_approval 节点写入）
    plan_revision_count: int            # 修订次数（≤3 上限）
    phase_retries: int                  # plan JSON 解析失败重试（≤2）
    todo_state: dict                    # step_id → "done"|"failed"（内存态；in_progress 由 current_step_index 推得）
    current_step_index: int             # 最近 todo_step start 的步序（0 起）
    explore_done: bool                  # P2 探索完成标记（tool_exec 观察 subagent wait_all 成功）
    plan_candidate: str                 # P3 Plan 子代理报告原文（tool_exec 观察 role=plan 完成）

    # 技能与工具
    active_skills: list[str]
    auto_skill: bool
    multi_table: bool
    file_ids: list[str]
    uploaded_files: list[dict]          # 会话上传文件 [{file_name,file_path,file_type}]
    # 归属三态（2026-08-24）：新会话复制的知识库 xlsx [{file_name,file_path}]（skill_router 注入 kb_xlsx 块）
    kb_xlsx_files: list[dict]
    kb_xlsx_truncated: bool             # 复制数量达到 cap（skill_router 附"只复制部分"说明）
    allowed_tools: list[str]            # 团队白名单

    # 对话
    system_prompt: str
    messages: list[dict]                # 原生 dict：{role,content,reasoning_content,tool_calls}
    dynamic_context: str                # 四期缓存优化：记忆/文件动态上下文（随当前轮 user 消息注入，system 静态化）
    request_user_content: str           # 实际发给 LLM 的当前轮 user 消息（含上下文；落库保持一致保缓存前缀）
    # 2026-09-15（上下文进度条）：首次 LLM 调用时的上下文分解（history/total 估算 + real=真实 prompt_tokens）；
    # persist_round 落 Redis 供进度条与压缩阈值使用
    ctx_probe: dict | None
    tool_round_count: int               # React 循环计数
    max_tool_rounds: int                # 循环上限（来自配置）
    no_progress_streak: int             # 无进展连续轮数（工具全部无有效输出时 +1，连续 4 轮终止——2026-08-07 设计）
    no_delivery_streak: int             # M1（2026-08-10）：无交付连续轮数（工具有输出但无交付物时 +1——==note_rounds 注入收敛注记、>=final_rounds 卡点收尾）
    converge_note: str | None           # M1：收敛注记（一次性——agent_llm/verify 注入到当前轮 user 消息后清空）
    force_final: bool                   # 连败/超限后强制出最终文本（无 tools）

    # 工具调用（v2 框架：工具直接执行，无授权卡）
    pending_tool_calls: list[dict]      # 模型本轮返回的工具调用（待执行）
    plan: str                           # 模型输出的「【计划】」文本（计划反问轮，非空 → 本轮结束等用户文字确认）

    # D20 计划状态机（2026-08-10）：sessions.plan_status 的图内镜像
    plan_status: str                    # none / pending / confirmed / rejected / expired
    plan_confirmed: bool                # 本 ask 已确认计划（skill_router 注入"直接执行"提示）
    plan_text: str                      # 待确认的计划文本（技能注入用）
    plan_expires_at: str | None         # 计划过期时间（ISO；plan_router 惰性检查）

    # 产出
    round_outputs: list[dict]           # [{type,label,chart_id,file_path,...}]
    tool_events: list[dict]             # 工具调用记录 [{tool_name,status,brief,detail}]（持久化）
    prior_tool_events: list[dict]       # D18：最近 2 轮工具过程摘要（_load_history 提取，skill_router 注入动态上下文）

    # 支柱 3（2026-08-10）：任务清单与总量护栏
    plan_tasks: list[str]               # 已确认计划的编号任务清单（parse_plan_tasks 现场解析，跨轮注入）
    ask_tool_chars: int                 # 单 ask 工具结果回填累积字符（跨 tool_exec 调用累积，超 ask_tool_chars_max 降级）

    # 2026-08-20（P1）：插话链路
    interrupts_pending: bool            # tool_exec/verify 探测到中断队列非空（_route_after_tool / _route_after_verify 读——收尾前优先回 agent_llm 消费）
    interrupt_reroute: int              # 本轮 ask 内"verify 因插话回 agent_llm"的次数（≤5，防队列取不动时无限回环）
    interrupt_log: list[dict]           # 本轮已消费的用户插话原文 [{message}]（persist_round 落库为 user 行，刷新后气泡可见）
    plan_done_streak: int               # 2026-08-20（走查修正）：计划任务全部完成后连续未收尾轮数（≥2 强制收尾，防"步骤完成仍无指示连跑复测"）

    # 安全
    input_filter_result: dict           # {passed, reason, rule}

    # 错误
    error: str | None
