"""Skill 注册表（四期重构）：工具元数据聚合门面，替代原 prompts/skills/*.yaml 技能层。

工具在 app/agent/tools/ 代码层注册（ToolSpec 携带元数据），此处提供：
- 工具元数据列表（/skills 功能栏展示）
- 工具提示词注入（build_skills_prompt：描述驱动，LLM 依据 description 自主选用，无关键词匹配）
- 工具名解析（resolve_tool_names）

团队技能（SKILL.md 文件，可含工具声明）由 app/services/skill_file_service.py 异步管理（查库）。
"""
from __future__ import annotations

from app.agent.tools import get_all_tool_meta, get_tool


def _tool_meta_list(include_hidden: bool = False) -> list[dict]:
    return [m for m in get_all_tool_meta(include_hidden=include_hidden) if m["status"] == "active"]


def get_all_skills(dept_id: str | None = None) -> list[dict]:
    """全部工具元数据（原技能列表语义）；dept_id 参数保留以兼容调用方。"""
    return _tool_meta_list()


def get_active_skills(dept_id: str | None = None) -> list[dict]:
    return _tool_meta_list()


def get_skill(skill_id: str, dept_id: str | None = None) -> dict | None:
    t = get_tool(skill_id)
    return t.to_meta() if t else None


def build_skills_prompt(enabled: list[str], auto: bool = False, dept_id: str | None = None,
                        extra_ids: set[str] | None = None) -> str:
    """组装注入 system prompt 的工具描述文本（无关键词匹配，由 LLM 依据 description 自主选用）。

    4.1：auto 模式不包含 select_mode=single 的工具（skill 类须用户明确单选）。

    extra_ids（2026-09-17 bug 修复）：**闸门额外放行的工具**（MCP 外部工具——它们 hidden=True、
    不在功能栏也进不了 enabled 集合），要把描述一并注入，否则模型拿得到工具却不知道该用它。
    """
    extra = extra_ids or set()
    metas = [m for m in _tool_meta_list(include_hidden=bool(extra))
             if (auto and m["select_mode"] == "multi") or m["id"] in enabled or m["id"] in extra]
    if not metas:
        return ""
    lines = ["## 你可用的工具", "以下是你当前可调用的工具，根据用户意图与各工具描述自主选用："]
    for m in sorted(metas, key=lambda x: (x["sort_order"], x["id"])):
        # 2026-09-18：不再带图标前缀。原先是 `- {emoji} {name}（id）：描述`——图标对模型选工具
        # 没有信息量，却把"表现"耦合进了提示词（改图标＝改提示词）。工具名与描述已足够。
        lines.append(f"- {m['name']}（{m['id']}）：{m['description']}")
    lines.append("优先判断用户意图属于哪个工具范畴；无法判断时直接与用户沟通澄清，不要臆断。")
    return "\n".join(lines)


def resolve_tool_names(enabled: list[str], auto: bool = False, dept_id: str | None = None) -> list[str]:
    """由勾选/自动模式解析允许的工具名集合（dept_id 参数保留兼容）。

    4.1：auto 模式只自动启用多选工具；single 工具（skill）须用户手动单选——
    auto 下用户勾选的 single 仍生效（互斥：多个时取第一个）。
    """
    if auto:
        picked = {m["id"] for m in _tool_meta_list() if m["select_mode"] == "multi"}
        single_picked = [
            m["id"] for m in _tool_meta_list() if m["select_mode"] == "single" and m["id"] in enabled
        ]
        if single_picked:
            picked.add(single_picked[0])
        return sorted(picked)
    picked = {m["id"] for m in _tool_meta_list() if m["id"] in enabled}
    # 单选工具互斥：仅保留勾选集合中的第一个（按 sort_order 稳定序）
    single_picked = sorted(
        (m["id"] for m in _tool_meta_list() if m["select_mode"] == "single" and m["id"] in picked),
        key=lambda x: next((m["sort_order"] for m in _tool_meta_list() if m["id"] == x), 99),
    )
    if single_picked:
        picked = picked - {m["id"] for m in _tool_meta_list() if m["select_mode"] == "single"} | {single_picked[0]}
    return sorted(picked)
