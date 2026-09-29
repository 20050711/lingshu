"""对话服务：Agent 运行编排 + SSE 事件流 + 落库。

流程：分配 round_id → 建 LangGraph 并 ainvoke（节点向事件队列发事件）
→ 队列消费为 SSE 帧 → 结束整段落库（chat_messages / chart_outputs）。
"""
from __future__ import annotations

import asyncio
import json
import time

from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import func, insert, text, update

from app.agent.graph import build_graph
from app.agent.llm_client import DeepSeekLLM
from app.agent.state import AgentState
from app.agent.tools import ToolContext
from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.logging import get_logger
from app.models import ChartOutput, ChatMessage, Session
from app.services.interaction_waiter import get_plan_waiter, get_question_waiter
from app.services.task_registry import TaskRecord, get_task, register, unregister
from app.services.events import EventBroadcaster, _frame_to_sse

logger = get_logger("services.chat_service")

_settings = get_settings()

END_MARKER = {"event": "__end__"}

# 升级轮固定衔接文本（定义在 services 层——chat.py 引用；persist_round 据此跳过
# 系统生成的 user 消息落库，2026-08-19 走查修复）
UPGRADE_QUESTION = "升级为复杂任务模式。请基于以上全部对话内容，重新梳理目标并进入复杂任务流程。"


async def run_agent(
    initial_state: AgentState,
    events: asyncio.Queue,
    output_dir: str,
) -> dict:
    """运行 Agent 图。返回最终 state（含产出物），供调用方落库。"""
    # 2026-08-20：QA 页主对话模型切换（aux_overrides._main）覆盖配置档——原 aux_overrides
    # 从未接线到引擎（用户选 pro 仍跑 flash，模型切换不生效的根因）
    aux_overrides = initial_state.get("aux_overrides") or {}
    _main = aux_overrides.get("_main") or {}
    llm = await DeepSeekLLM.create(
        role=initial_state["user_role"], dept_id=initial_state.get("department_id"),
        model_override=str(_main["model"]) if _main.get("model") else None,
        platform_override=str(_main["platform"]) if _main.get("platform") else None,
    )
    # D11（2026-08-14）：思考策略覆盖——显式选择（off/low/high/max）优先；
    # 未显式选择时：quick 默认关思考（快）、complex 沿用平台默认（质量保底）。
    # 2026-08-20：quick 默认关只覆盖"平台默认也未开思考"的场景——配置页 llm 档开了思考
    # （model_layer 默认档 → llm.thinking=True）时 quick 不再强制关（用户配置生效）
    thinking = initial_state.get("thinking")
    if thinking == "off":
        llm.thinking = False
    elif thinking in ("low", "high", "max"):
        llm.thinking = True
        llm.reasoning_effort = thinking
    elif thinking is None and initial_state.get("mode") == "quick" and not _settings.quick_default_thinking and not llm.thinking:
        llm.thinking = False
    # 知识库（2026-09-10）：可见知识库物理根（global/dept/u{uid} 三态预计算）
    kb_roots: list[str] = []
    try:
        from app.services.kb_service import get_visible_kb_roots

        kb_roots = await get_visible_kb_roots({
            "user_id": initial_state.get("user_id", 0),
            "dept_id": initial_state.get("department_id", ""),
            "role": initial_state.get("user_role", "employee"),
        })
    except Exception as e:
        logger.warning("知识库根预计算失败（降级空）: %s", str(e)[:100])
    ctx = ToolContext(
        session_id=initial_state["session_id"],
        round_id=initial_state["round_id"],
        department_id=initial_state["department_id"],
        user_role=initial_state["user_role"],
        client_id=initial_state.get("client_id", ""),
        output_dir=output_dir,
        user_id=initial_state.get("user_id", 0),
        kb_roots=kb_roots,
        # 同轮图表注册表：tool_exec 生成图表时写入，doc_export 同轮内嵌先查内存（DB 落库在轮末）
        chart_registry={},
        # 支柱 1（2026-08-10）：子代理执行体通道（主 LLM 同档实例 + 主事件队列）
        events=events,
        llm=llm,
        # 2026-08-14：用户级辅助模型覆盖（AI 技能管理页配置，initial_state 由 stream_ask 注入）
        aux_overrides=initial_state.get("aux_overrides") or None,
    )
    config = {
        "configurable": {
            "events": events,
            "llm": llm,
            "ctx": ctx,
            "question_waiter": get_question_waiter(),
            "plan_waiter": get_plan_waiter(),
        }
    }
    graph = build_graph()
    # 2026-09-01：Agent 图总超时兜底——任意节点内无超时 await（DB/Redis 挂起等）不再永久阻塞：
    # wait_for 超时抛 TimeoutError，由 _agent_runner 的 except 记录 error_message 并 finally 放 END_MARKER 收尾
    try:
        final_state = await asyncio.wait_for(
            graph.ainvoke(initial_state, config), timeout=_settings.agent_total_timeout_s)
    except asyncio.TimeoutError:
        logger.error("Agent 图总超时（>%ss）session=%s 强制收尾", _settings.agent_total_timeout_s,
                     str(initial_state.get("session_id", ""))[:8])
        raise
    return final_state


def _build_initial_state(
    session_id: str,
    round_id: int,
    question: str,
    user: dict,
    active_skills: list[str],
    auto_skill: bool,
    multi_table: bool,
    client_id: str,
    uploaded_files: list[dict] | None = None,
    mode: str = "quick",
    upgrade: bool = False,
    thinking: str | None = None,
    aux_overrides: dict | None = None,
    kb_xlsx_files: list[dict] | None = None,
    kb_xlsx_truncated: bool = False,
) -> AgentState:
    return {
        "session_id": session_id,
        "round_id": round_id,
        "user_question": question,
        "user_id": user["user_id"],
        "user_role": user["role"],
        "department_id": user["dept_id"],
        "client_id": client_id,
        "active_skills": active_skills,
        "auto_skill": auto_skill,
        "multi_table": multi_table,
        "uploaded_files": uploaded_files or [],
        # 归属三态（2026-08-24）：新会话复制的知识库 xlsx（skill_router 注入 kb_xlsx 块）
        "kb_xlsx_files": kb_xlsx_files or [],
        "kb_xlsx_truncated": kb_xlsx_truncated,
        "messages": [],
        "tool_round_count": 0,
        "max_tool_rounds": _settings.max_tool_rounds,
        "force_final": False,
        "round_outputs": [],
        "input_filter_result": {"passed": True},
        # v2 交互框架
        "mode": mode,
        "upgrade": upgrade,
        "thinking": thinking,
        "aux_overrides": aux_overrides or {},  # 2026-08-20：QA 页当次模型选择（含 _main 主模型覆盖）
        "question_round": 0,
        "asked_this_round": False,
    }


def _final_assistant_text(messages: list[dict]) -> str:
    """B3（D8）：取最后一条无 tool_calls 且有内容的 assistant 消息（草稿+终稿只留终稿）。

    普通轮 = verify 终稿；计划轮 = 计划文本；拒绝/超时收尾 = chat 节点终稿。
    """
    for m in reversed(messages):
        if m.get("role") == "assistant" and not m.get("tool_calls") and (m.get("content") or "").strip():
            return m["content"]
    return ""


def _tool_event_lines(tool_events, limit: int = 5) -> list[str]:
    """B7（D18）：从 tool_events 提取一行式摘要（跨轮追问的上下文依据）。"""
    lines: list[str] = []
    if isinstance(tool_events, str):  # 旧双重编码脏数据防御
        try:
            tool_events = json.loads(tool_events)
        except Exception:
            return lines
    for e in tool_events if isinstance(tool_events, list) else []:
        if not isinstance(e, dict):
            continue
        if e.get("kind") in ("intent", "result"):  # 2026-08-18：timeline 事件不进 LLM 跨轮上下文
            continue
        name = e.get("tool_name", "")
        status = e.get("status", "")
        brief = e.get("brief") or ""
        summary = e.get("summary") or brief  # B7：tool_exec 已写入 ≤800 字符 summary
        lines.append(f"- {name}({status}): {str(summary)[:200]}")
        if len(lines) >= limit:
            break
    return lines


async def _build_history_summary(
    session_id: str,
    dropped: list[tuple[int, list]],
    dept_id: str | None = None,
    user_role: str | None = None,
) -> str:
    """支柱 2（2026-08-10）：被丢轮次 → llm_aux 语义摘要（≤1500 字符）。

    - 输入：每轮 assistant 终稿 [:2000] + tool_events 摘要（≤800/轮），总输入 ≤10K（超限保留最近的被丢轮次）
    - llm_aux 档（task key history_summary，未配置回退 employee env）；60s 超时；失败返回空串（降级现状整轮丢弃）
    - Redis 缓存 hist_sum:v1:{session}:{boundary}（boundary=最新被丢轮次，轮次推进自动失效）
    """
    if not dropped:
        return ""
    boundary_rid = max(r for r, _ in dropped)
    key = f"hist_sum:v1:{session_id}:{boundary_rid}"
    try:
        from app.core.redis import redis_get

        cached = await redis_get(key)
        if cached:
            return cached
    except Exception:
        pass

    # 组装轻量表征：降序遍历（新→旧），超输入上限保留最近的被丢轮次（更接近原文层边界，信息更有用）
    block: list[str] = []
    total = 0
    for rid, items in sorted(dropped, key=lambda x: x[0], reverse=True):
        entry = [f"[第 {rid} 轮]"]
        for role, content, outputs, tool_events in items:
            if role == "assistant" and content:
                entry.append(f"答复：{str(content)[:2000]}")
        te_lines = _tool_event_lines(tool_events if isinstance(tool_events, list) else [], limit=3)
        if te_lines:
            entry.append("工具：" + "；".join(te_lines)[:800])
        s = "\n".join(entry)
        if block and total + len(s) > _settings.history_summary_input_max_chars:
            break
        block.append(s)
        total += len(s)
    if not block:
        return ""

    prompt = (
        "以下是某 AI 助手会话中早前轮次的精简记录（用户问题与工具过程摘要）。"
        "请用中文压缩为一段 ≤300 字的会话摘要，供助手后续轮次引用——必须包含："
        "用户的核心诉求、已完成的事项（含产出文件）、关键数据口径/结论、未完成或失败的事项。"
        "只输出摘要正文，不要标题、序号与时间戳。"
    )
    try:
        from openai import AsyncOpenAI

        from app.agent.llm_client import build_thinking_kwargs
        from app.services.config_service import get_aux_model_cfg_for_task

        cfg = await get_aux_model_cfg_for_task(dept_id, user_role, "history_summary")
        # 问题 11：平台感知 client（cfg 完整传入分流——不再写死 deepseek base_url）
        from app.agent.llm_client import create_text_client

        client, model, tkw = create_text_client(cfg)
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": "\n\n".join(block)}],
                max_tokens=500,
                **tkw,
            ),
            timeout=_settings.history_summary_timeout,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("历史摘要生成失败 session=%s: %s", str(session_id)[:8], str(e)[:100])
        return ""
    if not summary:
        return ""
    try:
        from app.core.redis import redis_set

        await redis_set(key, summary, _settings.history_summary_cache_ttl)
    except Exception:
        pass
    return summary


async def _load_history(
    session_id: str, max_rounds: int = 10, max_chars: int | None = None,
    dept_id: str | None = None, user_role: str | None = None,
    resume_hint: bool = False,
) -> tuple[list[dict], list[str], list[dict]]:
    """加载会话历史（跨轮记忆）：最近 N 轮 ChatMessage 按时间序构建 LLM 消息。

    persist_round 将 assistant 消息合并为文本落库，这里按 role/content 原样注入；
    计划文字确认（跨轮）依赖此机制——用户确认计划后 LLM 必须能看到上一轮的计划。
    返回 (msgs, output_summaries, tool_summaries)：B10 产出清单 + B7 最近 2 轮工具摘要
    （不含服务器路径，遵守 system 不输出 URL 纪律）。

    B6（D11）：按 (round_id) 分组自新向旧累积字符预算，超 history_max_chars 停止
    （至少保留最新一轮）；裁剪仅记日志，不注入"已裁剪"标记（防破坏缓存前缀稳定）。

    支柱 2（2026-08-10）：预算拆分——原文层（history_raw_ratio 比例）保留最近轮次原文，
    被丢弃的早期轮次经 llm_aux 语义摘要注入（分层压缩：
    some or all summarized + remaining unsummarized）。摘要字节跨 ask 稳定（Redis 缓存），
    仅"边界推进后首个 ask"首轮前缀 miss 一次（明示接受）。
    """
    from sqlalchemy import select

    from app.models import ChatMessage

    if max_chars is None:
        max_chars = _settings.history_max_chars
    layers_enabled = _settings.history_layers_enabled
    raw_budget = int(max_chars * _settings.history_raw_ratio) if layers_enabled else max_chars
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(ChatMessage.role, ChatMessage.content, ChatMessage.outputs,
                       ChatMessage.round_id, ChatMessage.tool_events)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(max_rounds * 2)
            )
        ).all()
    # 按 round 分组（最新在前），自新向旧保留预算内轮次
    by_rid: dict[int, list] = {}
    for role, content, outputs, rid, tool_events in rows:
        by_rid.setdefault(rid or 0, []).append((role, content, outputs, tool_events))
    # 2026-09-15（手动/被动压缩）：≤ boundary 的轮次由会话级结构化摘要代表，不再注入原文
    # （摘要不占原文预算；boundary 后新增轮次照常走原文层 + 预算裁剪）
    from app.services.context_service import get_summary as _ctx_get_summary

    compact = await _ctx_get_summary(session_id) or {}
    boundary = int(compact.get("boundary") or 0)
    order = [r for r in sorted(by_rid.keys(), reverse=True) if r > boundary]  # 最新在前
    kept_rids: list[int] = []
    used = 0
    for r in order:
        round_chars = sum(len(c) for _, c, _, _ in by_rid[r] if c)
        if kept_rids and used + round_chars > raw_budget:
            break
        kept_rids.append(r)
        used += round_chars
    dropped = [(r, by_rid[r]) for r in order if r not in kept_rids]
    if dropped:
        logger.info("历史注入裁剪：保留 %d 轮 / %d 字符（共 %d 轮）session=%s",
                    len(kept_rids), used, len(order), str(session_id)[:8])

    # 支柱 2：被丢轮次 → llm_aux 语义摘要（分层压缩；失败/关闭时降级为现状整轮丢弃）
    summary_block = ""
    if layers_enabled and dropped:
        summary_block = await _build_history_summary(session_id, dropped, dept_id, user_role)
        if summary_block:
            logger.info("历史分层压缩注入摘要 session=%s boundary=第 %d 轮", str(session_id)[:8], max(r for r, _ in dropped))

    msgs = []
    summaries: list[str] = []
    tool_summaries: list[dict] = []
    # 2026-09-15（压缩）：会话级压缩摘要块（覆盖第 1-boundary 轮；先于原文层注入）
    if compact.get("summary"):
        msgs.append({"role": "assistant",
                     "content": f"【会话压缩摘要（第 1-{boundary} 轮，已压缩）】\n{compact['summary']}"})
    if summary_block:
        min_r = min(r for r, _ in dropped)
        max_r = max(r for r, _ in dropped)
        msgs.append({"role": "assistant",
                     "content": f"【早期对话摘要（第 {min_r}-{max_r} 轮，已压缩）】\n{summary_block}"})
    for r in reversed(kept_rids):  # 旧→新（LLM 消息时序）
        for role, content, outputs, tool_events in by_rid[r]:
            if content and content.strip():
                msgs.append({"role": role, "content": content})
            # B10：产出清单（doc/image 产出，含可读 label）；parseJson 防御旧双重编码脏数据
            if role == "assistant" and outputs:
                if isinstance(outputs, str):
                    try:
                        outputs = json.loads(outputs)
                    except Exception:
                        outputs = None
                for o in outputs if isinstance(outputs, list) else []:
                    if isinstance(o, dict) and o.get("label") and o.get("type") in ("doc", "image", "chart"):
                        # 2026-08-14：注入真实 file_path（内部使用，Agent 跨轮读回产出必需——
                        # 磁盘名为 {uuid8}_{原名}，只给 label 时 Agent 猜文件名必失败；
                        # 路径不外泄由 system「回复不输出路径」纪律保证）
                        path = str(o.get("file_path") or "").strip()
                        line = f"- {o['type']}《{o['label']}》"
                        if path:
                            line += f" file_path：{path}"
                        summaries.append(line + "（可用 read_output 读回内容）")
            # B7：最近 2 轮（kept_rids 最新两个）的 assistant 工具事件摘要
            if role == "assistant" and tool_events and r in kept_rids[-2:]:
                lines = _tool_event_lines(tool_events)
                if lines:
                    tool_summaries.append({"round": r, "lines": lines})
    # 2026-08-17（问题 6 修复）：中断恢复注记——上轮被中断（降级落库已标记），
    # 本轮引导 LLM 先确认已产出再衔接，不把旧任务当新任务重做（"又生成一张"根因）
    # 2026-09-08 续跑升级：标记改 JSON（round/reason/outputs）；用户发「继续」类指令
    # 时强制"直接接续"，其余也提示按已有进展继续（修复第 1 问"中断后重来成本高"体验）
    try:
        from app.core.redis import redis_delete, redis_get

        interrupted = await redis_get(f"interrupted:{session_id}")
        if interrupted:
            await redis_delete(f"interrupted:{session_id}")
            _meta = {}
            try:
                _meta = json.loads(interrupted)
            except Exception:
                _meta = {"round": interrupted, "reason": "", "outputs": 0}
            _rid = str(_meta.get("round", "上一"))
            _reason = str(_meta.get("reason", "") or "")[:120]
            _outs = int(_meta.get("outputs") or 0)
            _asked = int(_meta.get("asked") or 0)
            if resume_hint:
                resume_line = ("用户本轮指令为「继续」类——**直接接续上一轮未完成的部分继续做**，"
                               "先回读已有产出（read_output / 上方【产出记录】）核对，然后往完成的方向推进。")
            else:
                resume_line = "请先确认已有产出是否满足用户要求：满足则直接总结交付；不满足则说明还差什么并继续完成。"
            msgs.append({
                "role": "user",
                "content": (
                    f"【系统注记】上一轮（第 {_rid} 轮）任务被中断"
                    + (f"（原因：{_reason}）" if _reason else "（中断原因见下方平台说明）")
                    + f"，该轮已产出 {_outs} 项产物（清单见上方【产出记录】，可用 read_output 读回）。"
                    + (f"上轮已向用户提问确认 {_asked} 次——本轮不要再调用 ask_user，按已有信息继续。"
                       if _asked else "")
                    + f"{resume_line}**不要再重复生成已存在的文件**。"
                ),
            })
    except Exception:
        pass
    return msgs, summaries, tool_summaries


async def _load_uploaded_files(session_id: str, file_ids: list[str] | None = None) -> list[dict]:
    """加载会话上传文件信息（供 Agent 引用真实路径）。

    文件为会话级：本次提问未指定 file_ids 时，自动带上该会话最近上传的文件
    （用户描述的数据流：上传后存团队上传目录 → 仅会话有效 → 路径给 Agent → 7 天随会话删除）。
    """
    from sqlalchemy import select

    from app.models import ChatFile

    engine = get_global_engine()
    query = select(ChatFile.file_name, ChatFile.file_path, ChatFile.file_type)
    if file_ids:
        # B11（D29）：显式 file_ids 也限 20（防刷爆上下文；Agent 可分批读）
        query = query.where(ChatFile.id.in_(file_ids[:20]), ChatFile.session_id == session_id).limit(20)
    else:
        query = query.where(ChatFile.session_id == session_id).order_by(ChatFile.created_at.desc()).limit(10)
    async with engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    return [
        {"file_name": r[0], "file_path": r[1], "file_type": r[2] or ""}
        for r in rows
    ]


async def _persist_charts(conn, session_id: str, round_id: int, charts: list[dict]) -> None:
    """图表落库（含 E-10 去重：同会话同 type+label 跨轮覆盖，同轮不删——同轮两张同名图并存）。

    降级落库（B4）与 persist_round 共用，保证断连轮已渲染图表可导出 PNG。
    """
    for o in charts:
        if not isinstance(o, dict) or o.get("type") != "chart":
            continue
        if o.get("label"):
            await conn.execute(
                text("DELETE FROM chart_outputs WHERE session_id=:sid AND chart_type=:ct "
                     "AND chart_label=:cl AND round_id < :cur_round"),
                {"sid": session_id, "ct": o.get("chart_type", ""), "cl": o.get("label", ""),
                 "cur_round": round_id},
            )
        await conn.execute(
            insert(ChartOutput).values(
                id=o.get("chart_id"),
                session_id=session_id,
                round_id=round_id,
                chart_label=o.get("label", ""),
                chart_type=o.get("chart_type", ""),
                option_json=o.get("option", {}),
            )
        )


async def _persist_plan_state(conn, session_id: str, round_id: int, final_state: dict) -> None:
    """B8（D20）：计划状态机转移（与消息落库同事务）。

    - 计划轮（final_state.plan 非空）→ pending（记录计划文本/轮号/过期时间）
    - 确认执行轮（plan_confirmed）→ none（复位）
    - v2 批准卡：plan_json 存在且未批准/已拒绝/超时 → pending（plan_json+revision 持久化，
      超时后用户下条消息按修订路径重开）；批准并执行完成 → 复位（revision 清零）
    """
    plan = final_state.get("plan")
    if plan:
        ttl = _settings.plan_expire_seconds
        await conn.execute(
            text("UPDATE sessions SET plan_status='pending', plan_text=:p, plan_round_id=:r, "
                 "plan_expires_at=NOW() + make_interval(secs => :ttl) WHERE id=:sid"),
            {"p": plan, "r": round_id, "ttl": ttl, "sid": session_id},
        )
        return

    # v2：复杂任务批准卡持久化（JSONB 直接传 dict——项目约定，勿 json.dumps）
    # 未批准（等待中/被拒/超时）的 plan_json 一律落 pending——超时或修订上限后卡片可跨 ask 重开
    plan_json = final_state.get("plan_json")
    plan_approval = final_state.get("plan_approval") or ""
    revision = final_state.get("plan_revision_count") or 0
    if plan_json and plan_approval in ("", "rejected", "timeout"):
        # 踩坑：text() 原生查询里 asyncpg 无法编码 dict 参数——必须 json.dumps 后 CAST 进 JSONB
        # （读回仍为 dict，不构成项目约定的"JSONB 双重编码"——那是指 ORM JSONB 列再 dumps）
        await conn.execute(
            text("UPDATE sessions SET plan_status='pending', plan_json=CAST(:j AS JSONB), "
                 "plan_revision_count=:rev, plan_expires_at=NULL WHERE id=:sid"),
            {"j": json.dumps(plan_json, ensure_ascii=False), "rev": revision, "sid": session_id},
        )
        return

    if final_state.get("plan_confirmed") or (plan_json and plan_approval == "approved"):
        await conn.execute(
            text("UPDATE sessions SET plan_status='none', plan_text=NULL, plan_round_id=NULL, "
                 "plan_expires_at=NULL, plan_json=NULL, plan_revision_count=0 WHERE id=:sid"),
            {"sid": session_id},
        )


async def persist_round(
    session_id: str,
    round_id: int,
    question: str,
    final_state: dict,
    uploaded_files: list[dict] | None = None,
) -> None:
    """流结束后落库：用户消息（含文件归属）+ assistant 消息（含工具调用记录与产出）。

    落库前过输出防护（output_guard）：确保记录中也不含 .env 密钥（与 SSE 转发层一致）。
    B3（D8）：assistant 只落最后一条无 tool_calls 内容（草稿+终稿不双重落库）。
    """
    from app.services.output_guard import guard_output

    messages = final_state.get("messages") or []
    # 2026-08-07：只落库"面向用户"的轮次内容——最终答复（verify/chat）与计划反问轮；
    # 工具调用轮次的中间说明（"我先读取…"+ 偶发 XML/代码幻觉）只进 LLM 上下文，不落库不回显
    # 问题 3：落库文本含工具标记 → 整段作废（与 done/verify/chat 同语义；strip 兜底残余）
    from app.core.text_utils import contains_tool_markup, strip_tool_xml

    assistant_text = strip_tool_xml(_final_assistant_text(messages))
    if contains_tool_markup(assistant_text):
        assistant_text = "任务已完成，请查看上方产出与工具执行情况。"
    assistant_text, _ = guard_output(assistant_text)
    outputs = final_state.get("round_outputs") or []
    tool_events = final_state.get("tool_events") or []

    engine = get_global_engine()
    # 四期缓存优化：落库与请求一致（含动态上下文的 user 消息）——历史注入原样 → 缓存前缀稳定
    user_content = final_state.get("request_user_content") or question
    async with engine.begin() as conn:
        # 2026-08-19（走查）：升级轮（question 为系统固定衔接文本）不落库 user 消息——
        # 否则前端刷新 get_messages 显示"用户没发过的消息"（用户可见性 bug）
        if user_content != UPGRADE_QUESTION:
            await conn.execute(
                insert(ChatMessage).values(
                    session_id=session_id,
                    role="user",
                    content=user_content,
                    round_id=round_id,
                    files=uploaded_files or [],
                )
            )
        # 2026-08-20（P1-b）：本轮已消费的插话原文落库为 user 行——插在问题之后、回答之前
        # （时序正确；刷新后插话气泡保留——原插话只在前端 store，任何重拉/刷新即消失）
        for it in final_state.get("interrupt_log") or []:
            await conn.execute(
                insert(ChatMessage).values(
                    session_id=session_id,
                    role="user",
                    content=str(it.get("message", ""))[:500],
                    round_id=round_id,
                )
            )
        if assistant_text:
            await conn.execute(
                insert(ChatMessage).values(
                    session_id=session_id,
                    role="assistant",
                    content=assistant_text,
                    round_id=round_id,
                    outputs=outputs,
                    tool_events=tool_events,
                )
            )
        await _persist_charts(conn, session_id, round_id, outputs)
        await _persist_plan_state(conn, session_id, round_id, final_state)
        # 需求 3（2026-08-17）：token 计费累计（deepseek 平台 usage 落库；列迁移见 scripts/migrate_cost.py）
        # 2026-08-17（价格换算）：细分缓存命中/未命中输入 + 输出 + 模型名（flash/pro 价格不同）
        sess_upd: dict = {"last_activity_at": func.now()}
        _tu = final_state.get("token_usage") or {}
        _ph = int(_tu.get("prompt_hit") or 0)
        _pm = int(_tu.get("prompt_miss") or 0)
        _co = int(_tu.get("completion") or 0)
        if _ph + _pm + _co > 0:
            sess_upd.update({
                "cost_tokens": Session.cost_tokens + _ph + _pm + _co,
                "cost_prompt_hit": Session.cost_prompt_hit + _ph,
                "cost_prompt_miss": Session.cost_prompt_miss + _pm,
                "cost_completion": Session.cost_completion + _co,
            })
            if _tu.get("model"):
                sess_upd["cost_model"] = str(_tu["model"])
        await conn.execute(update(Session).where(Session.id == session_id).values(**sess_upd))

    # 2026-09-15（上下文进度条）：水位统计落 Redis（ctx_probe 由 agent_llm 首轮记录；
    # 失败不影响落库主流程）
    try:
        from app.services import context_service

        probe = final_state.get("ctx_probe") or {}
        if probe:
            await context_service.save_stat(session_id, probe)
    except Exception as e:
        logger.warning("水位统计写入失败 session=%s err=%s", str(session_id)[:8], str(e)[:100])


_OUTPUT_TYPE_BY_EXT = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".mp4": "video", ".mov": "video", ".webm": "video",
    ".html": "html_report", ".htm": "html_report",
}


def _scan_round_outputs(session_id: str, round_id: int) -> list[dict]:
    """2026-08-17（问题 6）：中断时扫描本轮输出目录，恢复产出物记录（图片/视频/文件）。

    产出物已写盘（LLM 生成完成）但 DB 无记录——降级落库时补齐，前端可展示可下载。
    """
    from app.core.url_utils import output_url

    out_dir = Path(f"{_settings.output_dir}/{session_id}/{round_id}")
    if not out_dir.exists():
        return []
    outputs = []
    for p in sorted(out_dir.iterdir()):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        ftype = _OUTPUT_TYPE_BY_EXT.get(ext, "file")
        outputs.append({
            "type": ftype,
            "label": p.name,
            "file_path": str(p),
            "url": output_url(session_id, round_id, p.name),
        })
    return outputs


async def _persist_degraded(
    session_id: str, round_id: int, question: str, uploaded_files: list[dict] | None,
    charts_seen: list[dict], assistant_text: str = "",
) -> None:
    """B4（D9）：断连/拦截/内部异常降级落库——至少 user 消息 + 已产出图表 + last_activity。

    保证历史无空洞（下轮 LLM 能看到本轮问题）与预览区图表 PNG 可导出。失败仅记日志不阻塞收尾。
    assistant_text（2026-08-12，演示发现修复）：断连时已流出的 agent 文本一并落库——
    否则刷新后只见 user 消息、agent 回复丢失（1051f736 会话实测：降级落库只写 user 行）。
    2026-08-17（问题 6 修复）：① 产出保底——扫描本轮输出目录，图片/视频/文件产出物落库
    （原全丢→6 张孤儿图片无记录）；② 恢复注记——Redis 标记中断，下轮 _load_history 注入
    「上轮被中断」引导 LLM 衔接而非重做。
    """
    try:
        engine = get_global_engine()
        # 产出保底：扫描本轮输出目录（LLM 已生成但未落库的文件——图片/视频/文档等）
        recovered_outputs = _scan_round_outputs(session_id, round_id)
        async with engine.begin() as conn:
            await conn.execute(
                insert(ChatMessage).values(
                    session_id=session_id,
                    role="user",
                    content=question,
                    round_id=round_id,
                    files=uploaded_files or [],
                )
            )
            if assistant_text or recovered_outputs:
                await conn.execute(
                    insert(ChatMessage).values(
                        session_id=session_id,
                        role="assistant",
                        content=assistant_text or "（任务被中断，已产出部分文件，可下载查看）",
                        round_id=round_id,
                        outputs=recovered_outputs or [],
                    )
                )
            await _persist_charts(conn, session_id, round_id, charts_seen)
            await conn.execute(
                update(Session).where(Session.id == session_id).values(last_activity_at=func.now())
            )
        # 恢复注记：标记本会话上轮被中断（_load_history 下轮注入引导，防止 LLM 把旧任务当新任务重做）
        try:
            from app.core.redis import redis_set

            await redis_set(f"interrupted:{session_id}", str(round_id), 3600)
        except Exception:
            pass
        logger.info("降级落库完成 session=%s round=%d charts=%d assistant=%d outputs=%d",
                    str(session_id)[:8], round_id, len(charts_seen), len(assistant_text), len(recovered_outputs))
    except Exception as e:
        logger.warning("降级落库失败 session=%s round=%d err=%s", str(session_id)[:8], round_id, str(e)[:150])


async def legacy_stream_ask(
    session_id: str,
    round_id: int,
    question: str,
    user: dict,
    active_skills: list[str],
    auto_skill: bool,
    multi_table: bool,
    client_id: str,
    file_ids: list[str] | None = None,
    mode: str = "quick",
    upgrade: bool = False,
    thinking: str | None = None,
    aux_overrides: dict | None = None,
    kb_xlsx_files: list[dict] | None = None,
    kb_xlsx_truncated: bool = False,
    resume_hint: bool = False,
) -> AsyncIterator[str]:
    """SSE 事件流生成器（legacy，task_bg_enabled=False 回退路径）。yield SSE 帧；结束发 done；异常发 error。

    v2（2026-08-14）：首帧 mode 事件（双模式）；question/plan 等待在流内经交互等待器完成。
    2026-08-18：任务后台化启用后不再走此路径（断连取消 agent 的旧行为保留在此，供一键回退）。
    """
    t0 = time.monotonic()  # 4.1：本轮总耗时（done 事件携带，前端展示回复耗时）
    events: asyncio.Queue = asyncio.Queue()
    output_dir = f"{_settings.output_dir}/{session_id}/{round_id}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # v2：首帧广播模式（前端双按钮高亮）
    yield f"event: mode\ndata: {json.dumps({'mode': mode}, ensure_ascii=False)}\n\n"

    uploaded_files = await _load_uploaded_files(session_id, file_ids)
    initial = _build_initial_state(
        session_id, round_id, question, user, active_skills, auto_skill, multi_table, client_id,
        uploaded_files=uploaded_files, mode=mode, upgrade=upgrade, thinking=thinking,
        kb_xlsx_files=kb_xlsx_files, kb_xlsx_truncated=kb_xlsx_truncated,
    )
    # 跨轮记忆：注入最近历史（计划文字确认/引用前文都依赖；不注入当前轮未落库消息）
    # 支柱 2：补传 dept/role 供历史分层压缩摘要选 llm_aux 档
    history, output_summaries, tool_summaries = await _load_history(
        session_id, dept_id=user.get("dept_id"), user_role=user.get("role"),
        resume_hint=resume_hint,
    )
    if history:
        initial["messages"] = history
    # B10：历史产出清单注入（agent 跨轮可见自己生成的报告，配合 read_output 读回修改）
    if output_summaries:
        initial["prior_outputs"] = output_summaries
    # B7（D18）：最近 2 轮工具过程摘要注入（隔轮追问"刚才查的数据"有据可依）
    if tool_summaries:
        initial["prior_tool_events"] = tool_summaries
    # B8（D20）：计划状态机注入（plan_router 惰性过期检查 + skill_router 确认/作废提示）
    # v2：plan_json/plan_revision_count 注入（批准卡跨 ask 恢复/修订）
    async with get_global_engine().connect() as conn:
        sess_row = (
            await conn.execute(
                text("SELECT plan_status, plan_text, plan_expires_at, mode, plan_json, "
                     "plan_revision_count FROM sessions WHERE id=:sid"),
                {"sid": session_id},
            )
        ).first()
    # 2026-08-14：用户级辅助模型覆盖（AI 技能管理页卡片浮窗配置，system_config KV）注入 agent 上下文
    # 2026-08-20：QA 页当次选择（请求体 aux_overrides，含 _main 主模型）优先于 DB 用户覆盖
    from app.services.config_service import get_user_aux_overrides

    aux_ov = await get_user_aux_overrides(int(user.get("user_id") or 0))
    if aux_overrides:
        aux_ov = {**(aux_ov or {}), **aux_overrides}
    if aux_ov:
        initial["aux_overrides"] = aux_ov
    if sess_row:
        initial["plan_status"] = sess_row.plan_status or "none"
        initial["plan_text"] = sess_row.plan_text or ""
        initial["plan_expires_at"] = (
            sess_row.plan_expires_at.isoformat() if sess_row.plan_expires_at else None
        )
        if mode == "complex":
            initial["phase"] = "explore"  # v2：反问不焊死——澄清并入探索阶段（LLM 按需 ask_user）
            initial["plan_json"] = sess_row.plan_json
            initial["plan_revision_count"] = sess_row.plan_revision_count or 0
        else:
            initial["phase"] = "exec"  # quick 恒 exec（写门禁/verify 按 mode 判定）
    task = asyncio.create_task(run_agent(initial, events, output_dir))

    final_state: dict | None = None
    error_message: str | None = None

    async def _monitor() -> None:
        """监听 graph 完成，结束时放 END_MARKER。"""
        nonlocal final_state, error_message
        try:
            final_state = await task
        except asyncio.CancelledError:
            raise
        except Exception as e:
            error_message = f"Agent 执行异常: {str(e)[:300]}"
        finally:
            await events.put(END_MARKER)

    monitor_task = asyncio.create_task(_monitor())

    # 边跑边消费事件队列（流式）；15s 无事件发 heartbeat 保活（LLM thinking 阶段可能 40-60s）
    # S3 修复：防护在"事件出口"统一接入——
    #   text 流用重叠窗口缓冲（LLM 以 4 字符块输出，≥8 字符敏感值必完整落入某窗口后命中替换）；
    #   done/error/tool/chart/confirm 事件对序列化全文统一过滤（原 done 全文/工具 detail 均不过滤）。
    from app.services.output_guard import guard_output

    leaked_any = False
    text_pending = ""       # 未发送的 text 缓冲
    TEXT_WINDOW = 64        # 过滤窗口长度（须 ≥ 最长敏感值：deepseek key 35 字符，sk- 形态 13+；≤64 全部覆盖）
    TEXT_KEEP = 8           # 窗口间重叠字符数：≥8 字符敏感值跨边界时必被某窗口完整包含并整体替换
    intercepted = False     # M21：拦截轮（E010）短路标记
    charts_seen: list[dict] = []   # B4（D9）：本轮已发 chart 事件（断连/拦截降级落库用，保 PNG 可导出）
    try:
        while True:
            try:
                item = await asyncio.wait_for(events.get(), timeout=15)
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
                continue
            if item.get("event") == "__end__":
                break
            event_name = item["event"]
            payload = {k: v for k, v in item.items() if k != "event"}
            if event_name == "error" and payload.get("code") == "E010":
                intercepted = True
            if event_name == "chart":
                charts_seen.append(payload)
            if event_name == "text" and payload.get("delta"):
                text_pending += str(payload["delta"])
                while len(text_pending) >= TEXT_WINDOW:
                    window = text_pending[:TEXT_WINDOW]
                    emit_len = TEXT_WINDOW - TEXT_KEEP
                    filtered_all, hits = guard_output(window)
                    if hits:
                        leaked_any = True
                    payload = {"delta": filtered_all[:emit_len]}
                    yield f"event: text\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    text_pending = window[-TEXT_KEEP:] + text_pending[TEXT_WINDOW:]
                continue
            # 非 text 事件：序列化全文统一过滤（done/error/tool/chart/confirm 全覆盖）
            raw = json.dumps(payload, ensure_ascii=False)
            filtered_raw, hits = guard_output(raw)
            if hits:
                leaked_any = True
            yield f"event: {event_name}\ndata: {filtered_raw}\n\n"
    except asyncio.CancelledError:
        task.cancel()
        monitor_task.cancel()
        # 2026-08-10 根因修复（历史遗留：断连后该轮消息消失，刷新看不到）：
        # 客户端断连（uvicorn cancel scope）→ 生成器 await 点收 CancelledError → 任务仍处于
        # "取消中"状态（cancel 计数未清）→ **except 内再 await 会立即再次抛 CancelledError**
        # → 原 _persist_degraded 首行即被打断，降级落库永不执行（日志既无"完成"也无"失败"）。
        # 2026-08-12（BUG-1 复测仍失败，日志持续「降级落库被取消」）：uncancel（单次/循环）
        # 均无法阻止——uvicorn 在降级落库执行期间仍会再次 cancel 当前任务，await 点照抛。
        # 方案：降级落库移入独立任务（create_task 不受当前任务取消影响）+ shield 防取消传播，
        # 主任务 wait_for 最多 5s；超时则独立任务继续后台落库，主任务照常 raise。
        try:
            current = asyncio.current_task()
            while current is not None and current.cancelling() > 0:
                current.uncancel()
        except Exception:
            pass
        try:
            # 2026-08-12：断连降级也保留已流出的 assistant 文本（刷新后 agent 回复不丢）
            persist_task = asyncio.create_task(
                _persist_degraded(session_id, round_id, question, uploaded_files, charts_seen,
                                  assistant_text=text_pending)
            )
            await asyncio.wait_for(asyncio.shield(persist_task), timeout=5)
        except asyncio.TimeoutError:
            logger.info("降级落库转入后台 session=%s round=%d", str(session_id)[:8], round_id)
        except asyncio.CancelledError:
            logger.warning("降级落库等待被取消 session=%s round=%d（独立任务后台继续）",
                           str(session_id)[:8], round_id)
        raise
    except GeneratorExit:
        # 2026-08-10：另一条断连路径——StreamingResponse 关闭生成器（aclose）抛 GeneratorExit
        # （非 CancelledError），原代码不捕获 → user 消息同样丢失。同法降级落库。
        task.cancel()
        monitor_task.cancel()
        try:
            await _persist_degraded(session_id, round_id, question, uploaded_files, charts_seen,
                                    assistant_text=text_pending)
        except Exception as e:
            logger.warning("降级落库异常(GeneratorExit) session=%s round=%d err=%s",
                           str(session_id)[:8], round_id, str(e)[:150])
        raise

    if intercepted:
        # M21：拦截轮短路——不发 done（原 error 事件后仍发 done 表现矛盾，
        # 且历史 assistant 文本被重放落库逐轮膨胀）；error 事件已发出，前端据此终止。
        # 2026-08-12（BUG-3a 修复）：拦截轮不再降级落库——被拦原文若写 user 行，
        # 下一轮 _load_history 会把它注入 LLM 上下文，造成越狱内容泄漏；
        # 拦截审计已由 input_filter 写 SecurityEvent（account_id/hash/rule），此处只收尾不落库。
        logger.info("拦截轮短路 session=%s round=%d（不落库；SecurityEvent 已记录）", session_id, round_id)
        return

    # 流末尾 flush 剩余 text 缓冲（最后 ≤TEXT_KEEP 字符）
    if text_pending:
        filtered_tail, hits = guard_output(text_pending)
        if hits:
            leaked_any = True
        yield f"event: text\ndata: {json.dumps({'delta': filtered_tail}, ensure_ascii=False)}\n\n"

    if leaked_any:
        yield "event: text\ndata: " + json.dumps(
            {"delta": "\n\n> 检测到回答中出现疑似密钥信息，已自动屏蔽（如确需查看请检查配置）。"}, ensure_ascii=False
        ) + "\n\n"

    if error_message:
        # E-04(API)：内部异常不归 E005（限流语义），注册 E016"Agent 执行异常"；已产出内容降级落库
        yield f"event: error\ndata: {json.dumps({'code': 'E016', 'message': error_message}, ensure_ascii=False)}\n\n"
        await _persist_degraded(session_id, round_id, question, uploaded_files, charts_seen,
                                assistant_text=text_pending)
        return

    outputs = (final_state or {}).get("round_outputs") or []
    # B3（D8）：done payload 与 persist 同源——只取最后一条无 tool_calls 的 assistant 文本（草稿不重放）
    # 问题 3 修复（2026-08-17 绝对方案）：done 事件 message 含工具标记（未闭合截断也算）
    # → 整段作废（最后防线；源头 verify/chat 已作废，此处兜底防其他路径）
    from app.core.text_utils import contains_tool_markup

    final_text = _final_assistant_text((final_state or {}).get("messages") or [])
    if contains_tool_markup(final_text):
        final_text = "任务已完成，请查看上方产出与工具执行情况。"
    # B8（D20）：done 携带计划状态供前端刷新（plan 轮 → pending）
    fs = final_state or {}
    done_plan_status = (
        "pending" if fs.get("plan") else ("none" if fs.get("plan_confirmed") else None)
    )
    done_payload = {
        "message": final_text,
        "round_id": round_id,
        "outputs": [{k: v for k, v in o.items() if k != "option"} for o in outputs],
        "elapsed_s": round(time.monotonic() - t0, 1),  # 4.1：本轮总耗时（前端展示）
        "mode": mode,  # v2：双模式（前端会话级高亮同步）
    }
    if done_plan_status:
        done_payload["plan_status"] = done_plan_status

    # B4（D9）：persist 先于 done 事件（消除"done 已发后断连丢 persist"窗口）；persist 失败仍发 done
    try:
        await persist_round(session_id, round_id, question, final_state or {}, uploaded_files)
    except Exception as e:  # M12：裸吞改记日志 + 一次重试（整轮记录不再静默丢失）
        logger.error("persist_round 失败（首次）session=%s round=%s err=%s", session_id, round_id, str(e)[:200])
        try:
            await persist_round(session_id, round_id, question, final_state or {}, uploaded_files)
        except Exception as e2:
            logger.error("persist_round 重试仍失败 session=%s round=%s err=%s", session_id, round_id, str(e2)[:200])

    yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"


# =====================================================================================
# 会话任务后台化（2026-08-18，第 15 项）：任务与 SSE 请求生命周期解耦
#
# 运行模型：
#   POST /chat/ask → start_bg_task() 建 broadcaster + agent_task + relay_task（注册表登记）
#      agent_task: run_agent(graph) → finally queue.put(END_MARKER)
#      relay_task: 消费 queue（单一消费者）→ guard_output 过滤 → Redis 持久化 + 广播
#                  收 END_MARKER 后收尾（persist_round / done / aborted / E016 降级落库）
#   转发器 tail_stream（每连接一个）：回放 (last_seq+1..] 增量 → 尾随 live（断连只注销订阅）
#   seq = Redis list index（sse_events:{session}），前后端 seq 幂等去重保证不重不丢
# 关闭 task_bg_enabled 即回退 legacy_stream_ask（断连取消 agent 的旧行为）。
# =====================================================================================

_TASK_STATUS_SQL = "UPDATE sessions SET task_status=:s WHERE id=:sid"


async def _set_task_status(session_id: str, status: str) -> None:
    """会话任务状态落库（none/running/completed/error/interrupted）。失败仅记日志不阻塞收尾。"""
    try:
        async with get_global_engine().begin() as conn:
            await conn.execute(text(_TASK_STATUS_SQL), {"s": status, "sid": session_id})
    except Exception as e:
        logger.warning("task_status 更新失败 session=%s status=%s err=%s",
                       str(session_id)[:8], status, str(e)[:100])


async def _maybe_compact_before_ask(record: TaskRecord, initial: AgentState) -> None:
    """水位触发的被动压缩（2026-09-15）：水位 ≥ context_compact_pct% 且可压缩 → 先压缩再问答。

    - 压缩期间用户不能发新消息——本轮本就在等 agent，天然满足；事件广播"正在压缩上下文…"
    - 压缩后重载历史注入（本轮尚未开始）；任一步失败静默跳过（不阻断问答）
    """
    try:
        from app.services import context_service as ctx_svc

        budget = _settings.context_budget_tokens
        stat = await ctx_svc.get_stat(record.session_id)
        if not stat or budget <= 0:
            return
        if int(stat.get("watermark") or 0) * 100 < budget * _settings.context_compact_pct:
            return
        token = await ctx_svc.acquire_lock(record.session_id)
        if not token:
            return
        try:
            async with get_global_engine().connect() as conn:
                row = (await conn.execute(
                    text("SELECT s.department_id, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
                         "WHERE s.id = :sid"), {"sid": record.session_id})).first()
            dept_id, role = (row.department_id, row.role) if row else (None, None)
            await record.broadcaster.queue.put(
                {"event": "tool", "tool_name": "context_compact", "status": "progress",
                 "detail": "上下文水位已达到阈值，正在压缩历史…", "brief": "压缩上下文"})
            r = await ctx_svc.compact_session(record.session_id, dept_id, role, reason="watermark")
            if not r.get("ok"):
                logger.info("水位压缩跳过 session=%s: %s", str(record.session_id)[:8], r.get("error"))
                return
            history, output_summaries, tool_summaries = await _load_history(
                record.session_id, dept_id=dept_id, user_role=role)
            initial["messages"] = history
            if output_summaries:
                initial["prior_outputs"] = output_summaries
            if tool_summaries:
                initial["prior_tool_events"] = tool_summaries
            await record.broadcaster.queue.put(
                {"event": "tool", "tool_name": "context_compact", "status": "done",
                 "detail": f"已压缩至第 {r['boundary']} 轮", "brief": "上下文已压缩"})
            # 压缩记录入 state.tool_events（刷新后时间线仍可见；Redis 事件流只保 30 分钟）
            initial["tool_events"] = [*(initial.get("tool_events") or []), {
                "tool_name": "context_compact", "status": "done",
                "brief": "上下文已压缩", "detail": f"已压缩第 1-{r['boundary']} 轮为结构化摘要"}]
        finally:
            await ctx_svc.release_lock(record.session_id, token)
    except Exception as e:
        logger.warning("被动压缩失败（跳过，不阻断问答）session=%s err=%s",
                       str(record.session_id)[:8], str(e)[:120])


async def _agent_runner(record: TaskRecord, initial: AgentState, output_dir: str) -> None:
    """Agent 执行体：run_agent → 记结果 → finally 放 END_MARKER（relay 据此收尾）。"""
    try:
        # 2026-09-15（被动压缩）：水位 ≥ 阈值 → 本轮提问前先压缩（详见 _maybe_compact_before_ask）
        await _maybe_compact_before_ask(record, initial)
        record.final_state = await run_agent(initial, record.broadcaster.queue, output_dir)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        record.error_message = f"Agent 执行异常: {str(e)[:300]}"
    finally:
        await record.broadcaster.queue.put(END_MARKER)


async def _relay(
    record: TaskRecord, question: str, uploaded_files: list[dict] | None, t0: float,
) -> None:
    """收尾任务：消费 graph 事件 → 过滤/持久化/广播 → 收 END_MARKER 后落库收尾。

    生命周期独立于 SSE 连接（create_task 于 ask 时），断连只注销转发器，relay 必然执行到底。
    """
    bc = record.broadcaster
    intercepted = False
    while True:
        item = await bc.queue.get()   # 无超时：plan/question 交互等待可长达 900s（heartbeat 由转发器发）
        if item.get("event") == "__end__":
            break
        if item["event"] == "error" and item.get("code") == "E010":
            intercepted = True
        try:
            await bc.put(item)
        except Exception as e:
            # 2026-08-19（S1-2）：broadcaster 异常（如 Redis 挂起超时路径）不得杀死 relay
            # 单消费者——记日志继续消费，保证后续事件（含 END_MARKER 收尾）不丢
            # 2026-09-10（同类排查）：原引用 `sid`（在下一行才解包）→ 这条错误日志一旦触发就
            # NameError，反而把 relay 打死。改用已有对象 record.session_id。
            logger.error("broadcaster.put 异常 session=%s event=%s err=%s（relay 继续）",
                         str(record.session_id)[:8], item.get("event"), str(e)[:120])
    await bc.flush_text()

    sid, rid = record.session_id, record.round_id
    if intercepted:
        # M21：拦截轮短路——不发 done、不落库（SecurityEvent 已由 input_filter 记录；
        # 被拦原文若写 user 行，下轮 _load_history 注入会造成越狱内容泄漏）
        logger.info("拦截轮短路 session=%s round=%d（不落库；SecurityEvent 已记录）", str(sid)[:8], rid)
        record.status = "completed"
        unregister(record)
        return
    if record.terminated.is_set():
        record.status = "interrupted"
        await bc.put({"event": "aborted", "message": "任务已停止", "round_id": rid})
        await _persist_degraded(sid, rid, question, uploaded_files, bc.charts_seen,
                                assistant_text=bc.text_pending)
        await _set_task_status(sid, "interrupted")
        unregister(record)
        return
    if record.error_message:
        record.status = "error"
        await bc.put({"event": "error", "code": "E016", "message": record.error_message})
        await _persist_degraded(sid, rid, question, uploaded_files, bc.charts_seen,
                                assistant_text=bc.text_pending)
        await _set_task_status(sid, "error")
        unregister(record)
        return

    # 正常完成：persist 先于 done（原 stream_ask 语义）；persist 失败仍发 done
    try:
        await persist_round(sid, rid, question, record.final_state or {}, uploaded_files)
    except Exception as e:
        logger.error("persist_round 失败（首次）session=%s round=%s err=%s", str(sid)[:8], rid, str(e)[:200])
        try:
            await persist_round(sid, rid, question, record.final_state or {}, uploaded_files)
        except Exception as e2:
            logger.error("persist_round 重试仍失败 session=%s round=%s err=%s", str(sid)[:8], rid, str(e2)[:200])

    fs = record.final_state or {}
    final_text = _final_assistant_text(fs.get("messages") or [])
    from app.core.text_utils import contains_tool_markup

    if contains_tool_markup(final_text):
        final_text = "任务已完成，请查看上方产出与工具执行情况。"
    done_plan_status = (
        "pending" if fs.get("plan") else ("none" if fs.get("plan_confirmed") else None)
    )
    done_payload = {
        "message": final_text,
        "round_id": rid,
        "outputs": [{k: v for k, v in o.items() if k != "option"} for o in (fs.get("round_outputs") or [])],
        "elapsed_s": round(time.monotonic() - t0, 1),
        "mode": fs.get("mode") or "quick",
    }
    if done_plan_status:
        done_payload["plan_status"] = done_plan_status
    await bc.put({"event": "done", **done_payload})
    record.status = "completed"
    await _set_task_status(sid, "completed")
    unregister(record)


async def start_bg_task(
    session_id: str,
    round_id: int,
    question: str,
    user: dict,
    active_skills: list[str],
    auto_skill: bool,
    multi_table: bool,
    client_id: str,
    file_ids: list[str] | None = None,
    mode: str = "quick",
    upgrade: bool = False,
    thinking: str | None = None,
    aux_overrides: dict | None = None,
    kb_xlsx_files: list[dict] | None = None,
    kb_xlsx_truncated: bool = False,
    resume_hint: bool = False,
) -> TaskRecord:
    """启动后台任务（任务后台化主路径）：建 broadcaster/agent/relay 并注册。

    与 legacy_stream_ask 的准备工作（:627-680）保持一致——mode 帧、历史注入、
    计划状态、用户级辅助模型覆盖全部原样搬入。
    """
    t0 = time.monotonic()
    bc = EventBroadcaster(session_id, _settings.sse_events_ttl_seconds)
    # 任务起始 seq = 会话事件流当前末尾（跨轮连续；首连回放只取本任务事件，不混入旧轮残留）
    from app.core.redis import redis_llen

    try:
        bc.start_seq = (await redis_llen(f"sse_events:{session_id}")) - 1
        bc.seq = bc.start_seq
    except Exception:
        pass
    output_dir = f"{_settings.output_dir}/{session_id}/{round_id}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    uploaded_files = await _load_uploaded_files(session_id, file_ids)
    initial = _build_initial_state(
        session_id, round_id, question, user, active_skills, auto_skill, multi_table, client_id,
        uploaded_files=uploaded_files, mode=mode, upgrade=upgrade, thinking=thinking,
        kb_xlsx_files=kb_xlsx_files, kb_xlsx_truncated=kb_xlsx_truncated,
    )
    # 跨轮记忆：注入最近历史 + 产出清单 + 最近工具过程摘要（与 legacy 一致）
    history, output_summaries, tool_summaries = await _load_history(
        session_id, dept_id=user.get("dept_id"), user_role=user.get("role"),
        resume_hint=resume_hint,
    )
    if history:
        initial["messages"] = history
    if output_summaries:
        initial["prior_outputs"] = output_summaries
    if tool_summaries:
        initial["prior_tool_events"] = tool_summaries
    # 计划状态机注入（plan_router 惰性过期检查 + skill_router 确认/作废提示）
    async with get_global_engine().connect() as conn:
        sess_row = (
            await conn.execute(
                text("SELECT plan_status, plan_text, plan_expires_at, mode, plan_json, "
                     "plan_revision_count FROM sessions WHERE id=:sid"),
                {"sid": session_id},
            )
        ).first()
    from app.services.config_service import get_user_aux_overrides

    aux_ov = await get_user_aux_overrides(int(user.get("user_id") or 0))
    if aux_overrides:
        aux_ov = {**(aux_ov or {}), **aux_overrides}
    if aux_ov:
        initial["aux_overrides"] = aux_ov
    if sess_row:
        initial["plan_status"] = sess_row.plan_status or "none"
        initial["plan_text"] = sess_row.plan_text or ""
        initial["plan_expires_at"] = (
            sess_row.plan_expires_at.isoformat() if sess_row.plan_expires_at else None
        )
        if mode == "complex":
            initial["phase"] = "explore"
            initial["plan_json"] = sess_row.plan_json
            initial["plan_revision_count"] = sess_row.plan_revision_count or 0
        else:
            initial["phase"] = "exec"

    # mode 首帧（seq=0，经 relay 写流 + 广播——重连回放恢复双模式高亮）
    await bc.queue.put({"event": "mode", "mode": mode})
    record = TaskRecord(
        session_id=session_id, round_id=round_id, question=question,
        agent_task=None, relay_task=None,  # 先占位，create_task 后赋值
        broadcaster=bc,
    )
    record.agent_task = asyncio.create_task(_agent_runner(record, initial, output_dir))
    record.relay_task = asyncio.create_task(_relay(record, question, uploaded_files, t0))
    # ①（全局 Explore 排查）：relay 异常退出未 unregister → 同会话 409 永久卡死——
    # done_callback 兜底注销（正常路径已 unregister，get_task 校验防重复 pop）
    record.relay_task.add_done_callback(
        lambda _t: unregister(record) if get_task(record.session_id) is record else None)
    register(record)
    await _set_task_status(session_id, "running")
    logger.info("后台任务启动 session=%s round=%d", str(session_id)[:8], round_id)
    return record


async def tail_stream(record: TaskRecord, last_seq: int) -> AsyncIterator[str]:
    """转发器：回放 (last_seq+1..] 增量 + 尾随 live。断连（CancelledError/GeneratorExit）
    只注销订阅，agent/relay 任务继续后台执行。"""
    from app.core.redis import redis_lrange

    bc = record.broadcaster
    qid = bc.subscribe()
    try:
        # ① 回放持久化事件（Redis 流，按 index=seq 增量）
        # 回放起点 = max(调用方已消费 seq, 本任务起始 seq)——首连（last_seq=-1）只取
        # 本任务自身事件（会话流跨轮累积，不混入旧轮残留含旧 done）
        replay_start = max(last_seq, bc.start_seq)
        items = await redis_lrange(f"sse_events:{record.session_id}", replay_start + 1, -1)
        replayed_last = replay_start
        for raw in items:
            d = json.loads(raw)
            replayed_last = max(replayed_last, int(d.get("seq", 0)))
            yield _frame_to_sse({"event": d["event"], **d})
            if d.get("event") in ("done", "error", "aborted"):
                return
        # ② 尾随 live（丢弃已回放过的 seq——衔接窗口不重播；text 帧无 seq 不受此限）
        while True:
            try:
                frame = await asyncio.wait_for(bc.subs[qid].get(), timeout=15)
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
                continue
            if "seq" in frame and int(frame.get("seq", 0)) <= replayed_last:
                continue
            yield _frame_to_sse(frame)
            if frame.get("event") in ("done", "error", "aborted"):
                return
    finally:
        bc.unsubscribe(qid)   # 断连只注销转发器，任务继续


async def replay_stream(session_id: str, last_seq: int) -> AsyncIterator[str]:
    """任务已结束（或注册表无记录）时的回放：取 (last_seq+1..] 增量直到终态。

    流已被 TTL 清空（>30min）→ 立即 EOF——前端 onStale 兜底重拉 get_messages（DB 有该轮）。
    """
    from app.core.redis import redis_lrange

    items = await redis_lrange(f"sse_events:{session_id}", last_seq + 1, -1)
    for raw in items:
        d = json.loads(raw)
        yield _frame_to_sse({"event": d["event"], **d})
        if d.get("event") in ("done", "error", "aborted"):
            return
