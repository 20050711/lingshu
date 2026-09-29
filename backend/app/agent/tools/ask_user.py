"""ask_user 工具（v2，2026-08-14 用户决策：反问不焊死，交给模型分析）。

LLM 自主决定何时反问、问什么、问几轮——任务型需求"往死里反问"（动手前把
目标/范围/口径/成功标准问清，提示词驱动）。调用即弹反问浮窗卡（前端输入框上方），
等待用户回答（120s 超时按推荐项自动提交），回答以工具结果回填 LLM 继续循环。
"""
from __future__ import annotations

import json

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.core.logging import get_logger
from app.services.interaction_waiter import get_question_waiter

logger = get_logger("agent.ask_user")

_PARAMS = {
    "type": "object",
    "properties": {
        "purpose": {
            "type": "string",
            "description": "一句话说明为什么问（供卡片标题区展示，如「开始前需要确认几个关键口径」）",
        },
        "questions": {
            "type": "array",
            "description": "1-3 个反问（每个 2-4 个选项 + 推荐项）",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "问题文本（一句话，具体到用户原话的缺口）"},
                    "options": {"type": "array", "items": {"type": "string"},
                                "description": "2-4 个选项（第一个为推荐项；选项要具体，不要泛泛）"},
                    "multi_select": {"type": "boolean", "description": "是否可多选（默认 false）"},
                    "recommended": {"type": "array", "items": {"type": "integer"},
                                    "description": "推荐项下标（默认 [0]）"},
                },
                "required": ["text", "options"],
            },
        },
    },
    "required": ["questions"],
}

_FALLBACK_QUESTIONS = [{
    "text": "请确认是否按上述理解继续？",
    "options": ["继续执行", "我需要补充说明"],
    "multi_select": False,
    "recommended": [0],
}]


def _normalize(questions) -> list[dict]:
    out = []
    for q in (questions or [])[:3]:
        if not isinstance(q, dict):
            continue
        text = str(q.get("text") or "").strip()
        options = [str(o).strip() for o in (q.get("options") or [])][:4]
        if not text or len(options) < 2:
            continue
        out.append({
            "text": text,
            "options": options,
            "multi_select": bool(q.get("multi_select", False)),
            "recommended": [i for i in (q.get("recommended") or [0])
                            if isinstance(i, int) and 0 <= i < len(options)],
        })
    return out or _FALLBACK_QUESTIONS


def _default_answers(questions: list[dict]) -> list[dict]:
    return [
        {"index": i, "selected": list(q.get("recommended") or [0]), "other_text": ""}
        for i, q in enumerate(questions)
    ]


def _format_answers(questions: list[dict], answers: list[dict], extra_text: str, source: str) -> str:
    lines = [f"【用户回答（{'超时按推荐项' if source == 'timeout' else '用户提交'}）】"]
    by_index = {a.get("index", -1): a for a in answers}
    for i, q in enumerate(questions):
        a = by_index.get(i) or {}
        selected = [s for s in (a.get("selected") or []) if isinstance(s, int) and 0 <= s < len(q["options"])]
        chosen = "、".join(q["options"][s] for s in selected) if selected else "（未选择）"
        other = str(a.get("other_text") or "").strip()
        if selected and other:
            chosen = f"{chosen}（补充：{other}）"
        elif other:
            # 2026-08-31：前端「其他（自定义输入）」选项——不选已有选项、直接输入内容
            chosen = f"自定义：{other}"
        lines.append(f"Q{i + 1}「{q['text']}」→ {chosen}")
    if extra_text and extra_text.strip():
        lines.append(f"用户补充说明：{extra_text.strip()}")
    return "\n".join(lines)


async def _handler(args: dict, ctx: ToolContext) -> dict:
    questions = _normalize(args.get("questions"))
    waiter = get_question_waiter()
    question_id = waiter.new_question_id()

    # 广播反问卡事件（events 通道仅主循环存在；只读工具桥不注入本工具）
    if ctx.events is not None:
        await ctx.events.put({
            "event": "question",
            "question_id": question_id,
            "questions": questions,
            "timeout_s": waiter.timeout_seconds,
            "purpose": str(args.get("purpose") or "开始前需要确认几件事")[:60],
        })
    logger.info("ask_user 反问 session=%s qid=%s n=%d", str(ctx.session_id)[:8], question_id, len(questions))

    answered = await waiter.wait(
        ctx.session_id, question_id,
        default_answers=_default_answers(questions),
    )
    answers = answered.get("answers") or _default_answers(questions)
    extra_text = answered.get("extra_text") or ""
    source = answered.get("source") or "user"
    text = _format_answers(questions, answers, extra_text, source)
    return {"ok": True, "question_id": question_id, "answer": text, "source": source}


register_tool(ToolSpec(
    name="ask_user", progress_keys=("ok",),
    description=(
        "What：向用户提问（弹反问浮窗卡，等待用户选择回答后继续）。\n"
        "When：**任务型需求动手前把影响结果的关键点问清（能合理推断的不问）**——缺目标对象/缺范围（时间、团队、数据口径）/"
        "多义口径/成功标准不明/存在影响方案走向的取舍时，先问再干；回答后按答案继续，不重复追问。\n"
        "How：questions 给 1-3 问，每问 2-4 个具体选项（结合用户原话），recommended 标推荐项；"
        "purpose 一句话说明问什么。\n"
        "Result：返回用户的选择（【用户回答】文本）；超时未答自动按推荐项提交。\n"
        "约束：简单明确的需求（闲聊/单次查询）不要问；一次把关键点问全，避免挤牙膏式反复追问。"
    ),
    parameters=_PARAMS,
    queue="default",
    handler=_handler,
    display_name="向用户提问",
    icon="question",
    status="active",
    sort_order=93,
    group="系统",
    summary="反问浮窗卡（需求不清时问用户）",
    select_mode="multi",
))
