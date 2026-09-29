"""支柱 3a（2026-08-10）：任务清单解析与进度回显（按文本启发式跟踪步骤完成度）。

- 来源：计划确认发生在用户回复「可以」的同一 ask 内（plan_router 置 confirmed → 执行 → 轮末才复位），
  执行 ask 开始时 state.plan_text 必然存在 → 现场解析即可，零新表零新列。
- 解析：正则匹配行首编号行（system.py 已约束【计划】1-4 条编号格式）；<2 条整体视为 1 条。
  确定性、零 LLM 成本（不引入 LLM 提取——前缀稳定）。
- 进度：确定性启发式——任务文本前 6 字或引号内文件名出现在产出 label / 最近工具 brief 中 = 已完成。
  错标仅提示性（不阻断执行）；全部未匹配退化为 0/N。
"""
from __future__ import annotations

import re

_NUM_RE = re.compile(r"^\s*(\d+)[.、)）]\s*(.+)$")
_QUOTE_RE = re.compile(r"[「\"']([^」\"']+)[」\"']")


def parse_plan_tasks(plan_text: str) -> list[str]:
    """从已确认计划文本解析编号任务清单（如「1. 解析 xlsx；2. 生成 HTML」→ 2 项）。"""
    if not plan_text:
        return []
    tasks: list[str] = []
    for line in plan_text.splitlines():
        m = _NUM_RE.match(line)
        if m:
            tasks.append(m.group(2).strip())
    if len(tasks) < 2:
        flat = " ".join(l.strip() for l in plan_text.splitlines() if l.strip())
        return [flat] if flat else []
    return tasks


def task_progress(plan_tasks: list[str], round_outputs: list[dict] | None,
                  tool_events: list[dict] | None) -> str:
    """确定性启发式任务进度行（供 agent_llm 运行状态回显；错标仅提示性不阻断）。"""
    if not plan_tasks:
        return ""
    recent = " ".join(str(e.get("brief") or "") for e in (tool_events or [])[-5:])
    out_labels = " ".join(str(o.get("label") or "") for o in (round_outputs or []))
    done = 0
    current_idx: int | None = None
    for i, t in enumerate(plan_tasks):
        m = _QUOTE_RE.search(t)
        probe = m.group(1) if m else t[:6]
        if probe and (probe in out_labels or probe in recent):
            done += 1
        elif current_idx is None:
            current_idx = i
    if current_idx is None:
        current_idx = len(plan_tasks) - 1
    current = plan_tasks[current_idx]
    return f"任务进度：{done}/{len(plan_tasks)} 已完成（当前：{current_idx + 1} {current[:20]}）"
