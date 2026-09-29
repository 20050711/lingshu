"""verify 节点：最终答复前的自校验。

流程：agent_llm 给出候选答复（无 tool_calls）→ verify 以校验模式再调一次模型：
- 模型自检输出是否满足用户要求 → 不满足 → 返回 tool_calls（继续强化循环）
- 满足 → 输出最终答案（text 事件）→ END

校验轮数计入 tool_round_count（上限 5 次）。
"""
from __future__ import annotations

import asyncio

from langchain_core.runnables import RunnableConfig

from app.agent.llm_client import DeepSeekLLM
from app.agent.state import AgentState
from app.agent.tools import tools_to_schemas
from app.core.logging import get_logger

logger = get_logger("agent.node.verify")

VERIFY_INSTRUCTION = (
    "\n\n[自校验] 请检查你上一步的回答是否完整满足用户要求"
    "（数据是否齐全、格式是否正确、数量是否匹配、风格是否符合）。"
    "**重要：上面最后一条 assistant 消息是你的回答草稿，尚未发送给用户**——"
    "校验通过时，直接输出**一份**最终答案（可在草稿基础上小幅润色），"
    "**严禁重复输出**：不要复述草稿原文后再写一遍，不要输出两段相似内容——最终答案只保留一份；"
    "不要以'已经在上一条回复中输出'等表述开头，不要反问用户确认。"
    "如果答案已满足要求，直接输出最终答案（不要复述检查过程）。"
    "如果缺少数据或结果不符合要求，调用可用工具继续完善后再回答。"
    "若已成功调用生成类工具（图片/文档/表格），除非用户明确表达不满意，"
    "否则视为满足要求直接交付，不要重复生成。"
    "最终答案**严禁出现工具调用格式与代码原文**：不得包含 <tool_calls>、"
    "<invoke>、<function=…>、<parameter> 等任何工具调用标记，不得整段贴脚本代码——"
    "只输出面向用户的结论、要点与数据；需要提代码时最多引用一行关键片段。"
    "若本任务已进入执行收尾（复杂任务完成全部步骤，或快速任务完成交付），"
    "最终答案须包含业务四板块（用户是业务员工，不用开发术语）："
    "本次完成 / 依据与来源 / 暂未覆盖 / 后续建议"
    "（快速任务每板块 1-2 句简要版；复杂任务完整版）。"
)


async def run_verify(state: AgentState, config: RunnableConfig) -> dict:
    c = config["configurable"]
    events: asyncio.Queue = c["events"]
    llm: DeepSeekLLM = c["llm"]

    messages: list[dict] = list(state.get("messages") or [])
    system_prompt = state.get("system_prompt", "")
    user_question = state.get("user_question", "")

    allowed = state.get("allowed_tools") or []
    schemas = tools_to_schemas(allowed)
    # B5（D10 附加）：verify 轮与主请求同源（含动态上下文 ctx_block）——既保缓存前缀稳定，
    # 也消除"verify 看不到上下文块"导致的判断偏差
    verify_user_content = (state.get("request_user_content") or user_question) + VERIFY_INSTRUCTION
    # M4（2026-08-10）：计划已确认但本轮零交付且调过工具 → 追加交付检查（拦"LLM 直接回答但没产出"
    # 变体；21 轮事故本身无 verify 轮拦不住，作为兜底防线）
    # v2：complex 执行期（phase=execute 或 plan_confirmed）同语义生效
    if ((state.get("plan_status") == "confirmed"
         or (state.get("mode") == "complex" and (state.get("phase") or "") in ("execute", "final")))
            and not state.get("round_outputs")
            and state.get("tool_events")):
        verify_user_content += (
            "\n【交付检查】本任务计划已确认、你也已调用过工具，但当前没有产出任何交付文件/图表。"
            "若用户要求的是文件/报告/图表类交付，请先调用 doc_export / html_report / generate_chart "
            "或 run_script 的 mode=deliver 完成交付后再回答；仅当任务本就不需要文件交付时才直接回答。"
        )
    # 产出物审查（2026-08-28）：本轮有交付类产出时注入审查维度——与任务一致性/表格错匹配/
    # 留空值/html 布局异常；发现问题调工具修正或最终答案如实标注（graph 零改动，走 verify 现有 tool_calls 闭环）
    _round_outputs = state.get("round_outputs") or []
    if any(o.get("type") in ("doc", "image", "html", "chart") for o in _round_outputs):
        out_lines = "\n".join(f"- {o.get('label', '')}（类型 {o.get('type', '')}）" for o in _round_outputs[:10])
        verify_user_content += (
            "\n【产出物审查】本轮生成了交付物，最终答案前请审查产出物质量"
            "（必要时先用 file_parse 读取产出物内容核对）："
            "① 与用户任务是否一致（数据是否张冠李戴/答非所问）；"
            "② 表格类：行列错位、漏列、留空值；"
            "③ HTML/文档类：异常布局、占位符残留、乱码；"
            "④ 数量/指标是否与用户要求匹配。"
            "发现可修复问题 → 调用工具修正后重新交付；无法修复 → 在最终答案中如实向用户标注。\n"
            + out_lines
        )
    msgs = [{"role": "system", "content": system_prompt}, *messages, {"role": "user", "content": verify_user_content}]
    logger.info("node=verify enter msgs=%d", len(msgs))
    msg = await llm.ainvoke(msgs, tools=schemas or None, stream_cb=None)
    # 2026-09-08 计费补漏：verify 也是真实 LLM 调用，其 usage 必须累计——
    # 原实现只在 agent_llm 累计（chat_service.persist_round 从 token_usage 读），
    # verify 轮次的 token 计费被漏（前端显示金额偏小）
    _u = msg.get("usage")
    if _u and _u.get("prompt_tokens") is not None:
        _prev = state.get("token_usage") or {}
        _key = (llm.model if hasattr(llm, "model") else (_prev.get("model") or ""))
        state["token_usage"] = {
            "prompt_hit": int(_prev.get("prompt_hit") or 0) + int(_u.get("prompt_cache_hit_tokens") or 0),
            "prompt_miss": int(_prev.get("prompt_miss") or 0) + int(_u.get("prompt_cache_miss_tokens") or 0),
            "completion": int(_prev.get("completion") or 0) + int(_u.get("completion_tokens") or 0),
            "model": _key,
        }
    logger.info("node=verify exit tool_calls=%d content_len=%d", len(msg.get("tool_calls") or []), len(msg.get("content") or ""))

    if not msg.get("tool_calls") and msg.get("content"):
        # 校验通过：输出最终答案（打字机分块发送 + emoji 过滤）
        from app.core.text_utils import contains_tool_markup, strip_emoji

        content = strip_emoji(msg["content"])
        logger.info("node=verify intercept=%s content_len=%d", contains_tool_markup(content), len(content))
        # 2026-08-17（问题 3 绝对方案）：收尾轮输出含工具调用标记（未闭合截断也算）→
        # **整段作废**不发（用户永不看到工具调用文本），改用固定占位——不依赖正则剥离残余
        if contains_tool_markup(content):
            content = "任务已完成，请查看上方产出与工具执行情况。"
        chunk_size = 16  # P4（2026-08-10）：打字机节流（原 4 字符/15ms=66 次 setState/s，前端渲染+滚动卡顿）
        for i in range(0, len(content), chunk_size):
            await events.put({"event": "text", "delta": content[i : i + chunk_size]})
            if i + chunk_size < len(content):
                await asyncio.sleep(0.025)

    # 问题 3（2026-08-17）：返回消息含工具标记 → 整段作废（落库/done 与流式一致——
    # 原只剥流式文本，messages 追加原始 msg → done/落库取到原始 XML）
    from app.core.text_utils import contains_tool_markup

    if msg.get("content") and contains_tool_markup(msg.get("content") or ""):
        msg = {**msg, "content": "任务已完成，请查看上方产出与工具执行情况。"}
    # 2026-09-11（用户场景："总结的时候插了一句话，然后就不回复了"）：verify 是收尾前的最后一站，
    # 而消费插话只发生在 agent_llm。若用户在**生成答复期间**插话，本轮直接 END 会把插话留在
    # Redis 里直到下一次 ask（用户看到"发了消息没人理"）。这里补一次队列探测（同 tool_exec 口径），
    # 由 _route_after_verify 改道回 agent_llm 消费后再收尾。仅在本轮无待执行工具时探测。
    interrupts_pending = False
    reroute_n = int(state.get("interrupt_reroute") or 0)
    if not (msg.get("tool_calls") or []) and reroute_n < 5:   # 上限护栏：防队列取不动时无限回环
        try:
            from app.core.redis import redis_llen

            interrupts_pending = (await redis_llen(f"interrupt:{state.get('session_id', '')}")) > 0
            if interrupts_pending:
                logger.info("verify 收尾前发现未消费插话 → 回 agent_llm 消费 session=%s（第 %d 次）",
                            str(state.get("session_id", ""))[:8], reroute_n + 1)
        except Exception:
            interrupts_pending = False

    return {
        "messages": [*messages, msg],
        "pending_tool_calls": msg.get("tool_calls") or [],
        "tool_round_count": state.get("tool_round_count", 0),
        # M1：收敛注记一次性（agent_llm 注入后已清空，此处兜底——verify 走强化循环时防重复注入）
        "converge_note": None,
        # 插话滞留探测（见上）：True → _route_after_verify 回 agent_llm 而不是 END
        "interrupts_pending": interrupts_pending,
        "interrupt_reroute": reroute_n + (1 if interrupts_pending else 0),
    }
