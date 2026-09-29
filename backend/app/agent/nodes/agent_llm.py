"""agent_llm 节点：调用 DeepSeek（流式 text 事件 + tool_calls 解析）。

- 有 tool_calls 或输出「【计划】」标记 → 条件边走 confirm（计划确认）
- 无 tool_calls 且无计划 → 条件边走 END
- force_final（confirm 拒绝/超限后）→ 不带 tools 调用，直接出最终答复
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.llm_client import DeepSeekLLM
from app.agent.state import AgentState
from app.agent.tools import tools_to_schemas
from app.core.logging import get_logger

logger = get_logger("agent.node.agent_llm")

# 「【计划】...」标记：任务型请求的计划确认（新流程：计划以文字反问，不弹卡片）
_PLAN_RE = re.compile(r"【计划】\s*(.+?)(?=\n\n|$)", re.S)

# 计划反问固定行（后端统一追加，前端渲染为红色加粗；用户回复「可以」后下一轮执行）
PLAN_ASK_LINE = "**请确认以上计划：回复「可以」开始执行；如需调整请直接说明。**"


async def _stream_text(events: asyncio.Queue, text: str) -> None:
    """打字机输出（计划反问轮：文字直达用户，不经过 verify 再生成）。"""
    chunk_size = 16  # P4（2026-08-10）：打字机节流（原 4 字符/15ms=66 次 setState/s，前端渲染+滚动卡顿）
    for i in range(0, len(text), chunk_size):
        await events.put({"event": "text", "delta": text[i : i + chunk_size]})
        if i + chunk_size < len(text):
            await asyncio.sleep(0.025)


async def _drain_interrupts(session_id: str) -> tuple[str, list[dict]]:
    """回合边界消费中断队列（interrupt:{session} 全量原子 LPOP+DEL）。

    返回 (格式化注入文本, 已消费的用户插话原文列表)——文本空串=无中断。
    子代理完成通知（type=subagent）不返回原文：避免落库为伪用户消息（P1-b，2026-08-20）。
    Redis 不可用时走内存降级（单 worker 等价）。
    """
    from app.core.redis import redis_lpop_all

    try:
        items = await redis_lpop_all(f"interrupt:{session_id}")
    except Exception as e:
        # 2026-08-20（P1-④）：消费端 Redis 异常不再静默——走查时段无任何插话痕迹是
        # 排查最大障碍（端点与消费两端都零日志）
        logger.warning("中断队列消费失败 session=%s err=%s", session_id, e)
        items = []
    if not items:
        return "", []
    parts = []
    consumed: list[dict] = []
    for raw in items:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("type") == "subagent":
            parts.append(f"子代理 {d.get('agent', '')} 已完成：{d.get('summary', '')}")
        else:
            parts.append(f"用户中途插入新要求：{d.get('message', '')}")
            consumed.append({"message": str(d.get("message", ""))[:500]})
    logger.info("agent_llm 消费中断 %d 条 session=%s", len(items), session_id)
    if not parts:
        return "", consumed
    # SEC-06（2026-08-17）：【系统提醒】前缀弱化为【用户插话】——降低注入文本的权威性
    # （内容已在上游 interrupt 端点过 InputFilter，此为纵深）
    return "【用户插话】以下事件发生在本轮工具执行期间，请纳入考虑并适配：\n" + "\n".join(parts), consumed


async def run_agent_llm(state: AgentState, config: RunnableConfig) -> dict:
    c = config["configurable"]
    events: asyncio.Queue = c["events"]
    llm: DeepSeekLLM = c["llm"]
    logger.info("node=agent_llm enter msgs=%d tools=%d phase=%s", len(state.get("messages") or []), len(state.get("allowed_tools") or []), state.get("phase"))

    messages: list[dict] = list(state.get("messages") or [])
    # B1（D6 链二）：每次 LLM 调用前剔除未配对 tool_calls/tool 消息——checkpoint 确认清空
    # pending_tool_calls 但 messages 尾部 assistant(tool_calls) 无配对 tool 结果时，DeepSeek 直接 400
    from app.agent.nodes.chat import _prune_unpaired_tools

    messages = _prune_unpaired_tools(messages)
    system_prompt = state.get("system_prompt", "")
    user_question = state.get("user_question", "")

    # 四期缓存优化：动态上下文（记忆/文件）随当前轮 user 消息注入（system 已静态化）
    ctx_block = state.get("dynamic_context") or ""
    request_user_content = f"{ctx_block}\n\n{user_question}" if ctx_block else user_question

    # M1（2026-08-10）：运行状态回显 + 收敛注记注入（一次成功即失效）——
    # 21 轮事故根因之一：LLM 看不到自己在第几轮/已产出什么/连续几轮无交付，无自检锚点。
    # 注入后清空 converge_note（一次性）；落库 request_user_content 即注入版（B5 缓存前缀一致）。
    status_parts: list[str] = []
    tool_round_count = state.get("tool_round_count") or 0
    no_delivery = state.get("no_delivery_streak") or 0
    if tool_round_count > 0 or no_delivery > 0:
        outputs_n = len(state.get("round_outputs") or [])
        status_parts.append(
            f"【运行状态】已执行 {tool_round_count} 轮工具调用；已产出 {outputs_n} 项交付物；"
            f"最近连续 {no_delivery} 轮无交付进展。"
        )
    # 2026-09-08：反问次数可见（自评依据）——本回合已问过几次、上限多少
    _asked = int(state.get("asked_this_round") or 0)
    if _asked > 0:
        from app.agent.nodes.tool_exec import _ask_user_limit

        status_parts.append(
            f"本回合已向用户提问确认 {_asked} 次（上限 {_ask_user_limit(state.get('mode') or 'quick')}）"
            "——请基于已有信息执行，不要再调用 ask_user。"
        )
    # 支柱 3a（2026-08-10）：任务进度回显（确定性启发式，错标仅提示性，不影响执行）
    plan_tasks = state.get("plan_tasks") or []
    if plan_tasks:
        from app.agent.plan_tasks import task_progress

        progress = task_progress(plan_tasks, state.get("round_outputs") or [], state.get("tool_events") or [])
        if progress:
            status_parts.append(progress)
    converge_note = state.get("converge_note")
    if converge_note:
        status_parts.append(converge_note)
    if status_parts:
        # 2026-08-18：运行状态/收敛注记属动态上下文（仅供 LLM 自检，不对用户展示）——
        # 原直接拼在用户问题后且落库 → 前端用户消息显示多余"【运行状态】…"两行。
        # 包进【上下文开始】标记块（前端 stripCtx 剥离，现按全局 /g 匹配多块）
        status_block = f"【上下文开始】\n{chr(10).join(status_parts)}\n【上下文结束】"
        request_user_content = f"{request_user_content}\n\n{status_block}"

    # v2：阶段指令注入（plan_router 按 phase 生成，一次性——注入后清空防重复）
    phase_instruction = state.get("phase_instruction") or ""
    if phase_instruction:
        request_user_content = f"{request_user_content}\n\n{phase_instruction}"

    # 常规模式：注入本轮可用工具（L4：force_final 收尾分支已删除——graph 路由 force_final=True 时
    # 一律优先去 chat 节点，agent_llm 内分支不可达）
    # 中间轮次不流式输出（避免"一堆中间思考挤在消息里"）；
    # 最终回答统一由 verify 节点（校验通过）或 chat 节点（拒绝/超时）发出；
    # 计划反问轮例外：计划文本直接文字输出给用户（见下）。
    allowed = state.get("allowed_tools") or []
    schemas = tools_to_schemas(allowed)
    # v2：每回合开头消费中断队列（用户插话/子代理完成）——LPOP 全量+DEL 幂等，
    # 队空返回空串零开销；新 ask 首回合即消费（无活动流时积压的插话此时生效）。
    # 瞬态 user 消息注入本轮 msgs，不写 state.messages 不落库（保 B5 缓存前缀纪律）。
    # 流中插话：注入后**照做**（2026-09-11 走查两连否定"暂停"方案）——
    # ① 14:17 部署机：插话被 verify 吞掉（用户："他没收到我中间发的消息"）；
    # ② 16:37 部署机（改"暂停"之后）：模型回"请回复继续我再搜拼豆"，用户："回复被吃了，
    #    二次的需求工具也好像没用"——用户要的是**当场执行**，不是再等一轮确认。
    # 因此：不剥 tool_calls、不路由到 END，插话直接进本轮上下文并要求立刻办。
    # 子代理完成通知（subagent rpush）同样只注入不打断。
    interrupt_note, interrupt_consumed = await _drain_interrupts(state.get("session_id", ""))
    msgs = [{"role": "system", "content": system_prompt}, *messages]
    if interrupt_note:
        _note = (f"{interrupt_note}\n\n（系统：用户的新要求**直接在本轮执行**——需要工具就调用工具，"
                 "不要停在这句确认上、不要只回一句「收到」；新要求与当前任务冲突时以新要求为准；"
                 "原任务已完成的不用再补，优先把新要求办完并给出结果。）")
        msgs.append({"role": "user", "content": _note})
    msgs.append({"role": "user", "content": request_user_content})
    # 2026-09-15（上下文进度条）：本 ask 首次调用时记录上下文分解——
    # history=历史注入部分（首轮 messages 即历史）、total=全量估算；real 用 API usage 回填（更准）
    ctx_probe = state.get("ctx_probe")
    if not ctx_probe:
        from app.services.token_counter import estimate_messages_tokens

        ctx_probe = {"history": estimate_messages_tokens(messages), "total": estimate_messages_tokens(msgs)}
    msg = await llm.ainvoke(msgs, tools=schemas or None, stream_cb=None)

    # 需求 3（2026-08-17）：token 计费累计——仅当 API 返回 usage（deepseek 平台）时累计；
    # 非 deepseek 模型（agnes 文本/GLM）或 usage 缺失（mock/异常）不计费（前端显示未知）
    # 2026-08-17（价格换算）：细分缓存命中/未命中输入 + 输出 + 模型名（flash/pro 价格不同）
    _u = msg.get("usage")
    if _u and _u.get("prompt_tokens") is not None:
        _prev = state.get("token_usage") or {}
        state["token_usage"] = {
            "prompt_hit": int(_prev.get("prompt_hit") or 0) + int(_u.get("prompt_cache_hit_tokens") or 0),
            "prompt_miss": int(_prev.get("prompt_miss") or 0) + int(_u.get("prompt_cache_miss_tokens") or 0),
            "completion": int(_prev.get("completion") or 0) + int(_u.get("completion_tokens") or 0),
            "model": llm.model if hasattr(llm, "model") else (_prev.get("model") or ""),
        }
    # 2026-09-15（上下文进度条）：首轮真实 prompt_tokens 回填 ctx_probe（比本地估算准）
    if ctx_probe.get("real") is None and _u and _u.get("prompt_tokens"):
        ctx_probe = {**ctx_probe, "real": int(_u["prompt_tokens"])}

    # 计划确认（新流程：文字反问，不弹卡片）：解析「【计划】」标记
    content = msg.get("content") or ""
    plan = ""
    m = _PLAN_RE.search(content)
    if m:
        plan = m.group(1).strip()

    if plan:
        # 计划反问轮：计划文本流式输出给用户（去【计划】标记）+ 后端固定反问行
        # 2026-08-07：输出前剥离工具 XML 幻觉块（LLM 偶发把 <tool_calls> 写进计划文本）
        from app.core.text_utils import contains_tool_markup, strip_tool_xml

        display = strip_tool_xml(content.replace(m.group(0), plan).strip())
        logger.info("node=agent_llm plan_branch plan=%.50r has_xml=%s display_len=%d", plan, contains_tool_markup(content), len(display))
        await _stream_text(events, display)
        await _stream_text(events, f"\n\n{PLAN_ASK_LINE}")
        # 计划未确认：移除任何工具调用（防未配对 tool_calls 导致下轮 DeepSeek 400；
        # 同时防止未确认就执行产出工具），本轮结束等用户下一条消息确认
        msg = {k: v for k, v in msg.items() if k != "tool_calls"}
        # content 保留计划文本落库（历史可见，下一轮 LLM 自行判断用户是否确认）

    # 2026-09-11：插话**不再**剥工具调用——用户要的是当场执行（见上方注释），
    # 剥掉会让模型"只确认不动手"，正是 16:37 那次"回复被吃了"的成因。

    new_messages = [*messages, msg]
    logger.info("node=agent_llm exit plan=%r pending_tool=%d content_len=%d", plan, len(msg.get("tool_calls") or []), len(content))
    return {
        "messages": new_messages,
        "tool_round_count": state.get("tool_round_count", 0),
        # 需求 3：token 计费累计值（累计于本节点 ainvoke 后）
        "token_usage": state.get("token_usage"),
        # 2026-09-15：上下文分解探针（首轮记录，persist_round 落 Redis 供进度条/压缩阈值）
        "ctx_probe": ctx_probe,
        # 计划轮无待执行工具（等确认）；插话轮**照常返回工具调用**（当场执行，2026-09-11 修正）
        "pending_tool_calls": [] if plan else (msg.get("tool_calls") or []),
        "plan": plan,
        # B5（D10）：实际请求内容写入 state——persist 落库与之保持一致（缓存前缀稳定，
        # 否则历史注入与上轮实际请求字节不一致 → DeepSeek 上下文缓存前缀 miss）
        "request_user_content": request_user_content,
        # M1：收敛注记一次性（注入后清空，防 verify 后强化循环重复注入）
        "converge_note": None,
        # v2：阶段指令一次性（plan_router 每阶段只生成一次）
        "phase_instruction": None,
        # 2026-08-20（P1-b）：本轮已消费的用户插话原文——persist_round 落库为 user 行
        # （刷新后插话气泡保留；拦截轮短路不落库，接受该轮插话仅内存可见）
        "interrupt_log": [*state.get("interrupt_log", []), *interrupt_consumed],
    }
