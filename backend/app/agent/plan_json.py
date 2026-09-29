"""计划 JSON 校验与 todo 状态合并（v2 复杂任务，2026-08-14）。

schema（设计书 §15.2.5）：
{goal, steps[{id,title,intent,verify}], risks[], questions_answered[]}
"""
from __future__ import annotations

import json
import re
from app.core.text_utils import parse_llm_json


def validate_plan_json(data) -> tuple[dict | None, str]:
    """校验并规范化计划 JSON。返回 (计划, 错误)；失败返回 (None, 原因)。"""
    if not isinstance(data, dict):
        return None, "计划不是 JSON 对象"
    goal = str(data.get("goal") or "").strip()
    if not goal:
        return None, "缺 goal 字段"
    steps = data.get("steps")
    if not isinstance(steps, list) or not (2 <= len(steps) <= 8):
        return None, f"steps 须为 2-8 条（实际 {len(steps) if isinstance(steps, list) else '非数组'}）"
    norm_steps = []
    seen_ids: set[str] = set()
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            return None, f"steps[{i}] 不是对象"
        sid = str(s.get("id") or "").strip()
        if not re.fullmatch(r"s\d+", sid):
            return None, f"steps[{i}] id 非法（须 s1..sn 稳定编号，实际 {sid!r}）"
        if sid in seen_ids:
            return None, f"步骤 id 重复：{sid}"
        seen_ids.add(sid)
        title = str(s.get("title") or "").strip()
        intent = str(s.get("intent") or "").strip()
        verify = str(s.get("verify") or "").strip()
        if not title or not intent or not verify:
            return None, f"steps[{i}] 缺 title/intent/verify"
        norm_steps.append({"id": sid, "title": title, "intent": intent, "verify": verify})
    risks = [str(r).strip() for r in (data.get("risks") or []) if str(r).strip()]
    qa = [str(q).strip() for q in (data.get("questions_answered") or []) if str(q).strip()]
    return {
        "goal": goal,
        "steps": norm_steps,
        "risks": risks,
        "questions_answered": qa,
    }, None


def parse_plan_json(text: str) -> tuple[dict | None, str]:
    """从 Plan 子代理报告解析计划 JSON（容忍 markdown 代码围栏与前后杂文）。"""
    if not text:
        return None, "报告为空"
    content = text.strip()
    # 剥 markdown 围栏
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.S)
    if m:
        content = m.group(1)
    else:
        # 无围栏：取首个 { 到末个 } 区间（容忍前后文字）
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None, "未找到 JSON 对象"
        content = content[start : end + 1]
    try:
        data = parse_llm_json(content)  # 2026-09-01：统一容错（免费档 markdown 包装）
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败：{str(e)[:80]}"
    return validate_plan_json(data)


def merge_todo_state(old_todo: dict, new_plan_json: dict) -> dict:
    """修订重提计划后合并 todo：按 step_id 保留已完成项；失效 id 丢弃。

    返回 (todo_state, completed_step_ids)。
    """
    new_ids = {s["id"] for s in new_plan_json.get("steps", [])}
    merged = {k: v for k, v in (old_todo or {}).items() if k in new_ids and v == "done"}
    return merged
