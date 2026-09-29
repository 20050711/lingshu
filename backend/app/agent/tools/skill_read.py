"""技能说明按需读取工具（2026-08-26，Q8：skill 提示词瘦身）。

改造背景：技能正文原全量常驻 system prompt（团队 ≤8000 + 全局 ≤4000 字符）。
改为清单注入（名字+简介）后，LLM 按名调用本工具获取完整说明与脚本清单。

安全（对齐 skill_router 注入语义）：
- 可见范围三层交集：团队技能过员工偏好、全局技能过团队开关+员工偏好
  （复用 list_active_skills / list_active_global_skills，与注入路径一致）
- skill_dir 非空时实时读盘（复用 _sync_skill_from_file：文件为准 + 变化同步 DB + audit）
- 返回内容过 InputFilter 注入检测（不过则报不合规，不返回原文）
"""
from __future__ import annotations

from app.agent.tools import ToolContext, ToolSpec, register_tool

# 按需取全文上限（比常驻 1500 宽裕：按需加载本就为省常驻 token，取一次给足）
_MAX_BODY_ONDEMAND = 8000
# 返回脚本清单展示上限
_MAX_SCRIPT_NAMES = 20


async def run_skill_read(args: dict, ctx: ToolContext) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "参数无效：name 必填（技能清单中的技能名）"}

    from app.core.input_filter import InputFilter
    from app.services.skill_file_service import (
        _skill_root,
        _sync_skill_from_file,
        list_active_global_skills,
        list_active_skills,
    )

    operator = f"user{ctx.user_id}@{str(ctx.session_id or '')[:8]}"
    # 可见范围与注入路径一致：团队技能 ∩ 员工偏好 ∪ 全局技能（团队开关 ∩ 员工偏好）
    skills = await list_active_skills(ctx.department_id, ctx.user_id or 0)
    dept_skill = next((s for s in skills if s["name"] == name), None)
    if dept_skill is None:
        gskills = await list_active_global_skills(ctx.department_id, ctx.user_id or 0)
        gskill = next((s for s in gskills if s["name"] == name), None)
        if gskill is None:
            return {"error": f"技能「{name}」不存在或不可用，请从技能清单中选择"}
        s, scope = gskill, "global"
    else:
        s, scope = dept_skill, "dept"

    # 实时读盘同步（文件为准；解析失败返回不可用——与注入路径一致，不静默回退 DB）
    if not await _sync_skill_from_file(s, scope, operator):
        return {"error": f"技能「{name}」文件不可用（解析失败），请联系管理员检查技能文件"}

    desc = s.get("description") or ""
    body = s.get("body") or ""
    # SEC-09：注入检测（与 build_skill_files_prompt 一致）
    filt = InputFilter.check(f"{desc}\n{body}", ctx.department_id)
    if not filt["passed"]:
        return {"error": f"技能「{name}」内容不合规，已屏蔽（请联系管理员检查）"}

    if len(body) > _MAX_BODY_ONDEMAND:
        body = body[:_MAX_BODY_ONDEMAND] + f"\n…（正文过长，已截断至前 {_MAX_BODY_ONDEMAND} 字）"

    tools = s.get("tools")
    # 脚本清单（供 run_script file= 执行；与常驻注入格式一致）
    # 2026-08-31（信息完备性 G2）：返回绝对路径——run_script 要求绝对路径且过白名单，
    # 相对路径/{技能目录} 占位符 agent 无法解析 → 技能脚本执行链断裂
    scripts: list[str] = []
    if s.get("skill_dir"):
        script_dir = _skill_root() / s["skill_dir"] / "scripts"
        if script_dir.is_dir():
            try:
                scripts = sorted(
                    str(p)
                    for p in script_dir.rglob("*") if p.is_file()
                )
            except OSError:
                scripts = []
    note = ("执行说明：按 body 指令逐步执行；有脚本时用 run_script 执行（mode=run lang=按脚本类型 "
            "file='{技能目录}/scripts/<脚本名>'，脚本 cwd 是会话工作目录，读技能内文件用 __file__ 所在目录或绝对路径）；"
            "中间产物写沙盒工作目录，最终交付用 mode=deliver 或 doc_export。")
    return {
        "name": s["name"],
        "scope": scope,
        "description": desc,
        "body": body,
        "tools": tools or [],
        "skill_dir": s.get("skill_dir"),
        "has_scripts": bool(scripts),
        "scripts": scripts[:_MAX_SCRIPT_NAMES] or [],
        "note": note,
    }


register_tool(
    ToolSpec(
        name="skill_read", progress_keys=("body", "scripts", "content", "description"),
        display_name="技能说明",
        icon="book-open",
        summary="按技能名获取完整技能说明与脚本清单",
        group="其他",
        sort_order=99,
        select_mode="multi",
        write=False,
        user_description="当技能清单中的技能名命中用户需求时，必须先调用本工具获取完整执行说明（正文与脚本清单）再执行。",
        description=(
            "What：按技能名获取完整技能说明（SKILL.md 正文、可用工具、脚本清单）。\n"
            "When：系统提示词中的「团队技能/全局技能」清单出现与用户需求匹配的技能名时，"
            "**必须先调用本工具**获取完整执行说明，再按说明执行技能（脚本经 run_script file= 执行）。\n"
            "How：name 传技能清单中的技能名（如 weekly-report-generator）。\n"
            "Result：返回 name/description/body（执行指令）/tools/scripts（脚本清单与执行引导）。\n"
            "**边界：技能不存在或脚本缺失 → 如实说明并给出可用的技能清单，不要编造能力或脚本名。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "技能名（来自系统提示词中的技能清单）"},
            },
            "required": ["name"],
        },
        queue="default",
        handler=run_skill_read,
    )
)
