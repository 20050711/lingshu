"""skill_router 节点：组装系统提示词 + 解析本次可用的工具集。

按 active_skills/auto_skill 解析工具白名单（Agent 空间隔离：仅授权工具可注入模型）。
四期重构：无关键词匹配（用户明确要求）——LLM 依据工具描述自主选用；
run_script 为兜底沙盒工具，恒注入（不依赖勾选、不受团队白名单限制）。
团队技能（SKILL.md）description+正文常驻注入。
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.prompts.system import build_system_prompt
from app.agent.skill_registry import build_skills_prompt, resolve_tool_names
from app.agent.state import AgentState
from app.services.dept_service import get_dept_name

logger = logging.getLogger("agent.skill_router")


def _zip_unzip_block(file_path: str, limit: int = 100) -> str:
    """zip 上传自动解压目录现场探测：返回注入清单文本；目录不存在（未解压/已清理）=自愈返回空。

    解压产物不入 ChatFile（防 50 个/会话上限撑爆），agent 可见性靠此处清单注入。
    """
    try:
        from pathlib import Path

        p = Path(file_path)
        unzip_dir = p.parent / f"{p.stem}_unzip"
        if not unzip_dir.is_dir():
            return ""
        files = sorted(str(f.relative_to(unzip_dir)) for f in unzip_dir.rglob("*") if f.is_file())
        if not files:
            return f"（已自动解压至 {unzip_dir.name}/ 目录，无文件）"
        head = files[:limit]
        sample = "、".join(head)
        more = f" 等共 {len(files)} 个文件" if len(files) > limit else f"（共 {len(files)} 个文件）"
        return (f"（已自动解压至 {unzip_dir.name}/ 目录：{sample}{more}——"
                f"需读取时把完整服务器路径传给 file_parse，解压目录为 {unzip_dir}）")
    except Exception as e:
        logger.warning("zip 解压目录探测失败: %s", str(e)[:100])
        return ""


async def _build_memory_context(user_id: int, dept_id: str) -> str:
    """合并注入团队记忆 + 个人记忆（四期重构：所有业务角色两类都注入），失败不阻塞。"""
    try:
        from app.services.memory_service import get_active_for_dept, get_user_memories

        parts: list[str] = []
        dept_mems = await get_active_for_dept(dept_id)
        if dept_mems:
            parts.append("【团队记忆】\n" + "\n".join(f"- {m['content']}" for m in dept_mems))
        user_mems = await get_user_memories(user_id)
        if user_mems:
            parts.append("【个人记忆】\n" + "\n".join(f"- [{m['type']}] {m['content']}" for m in user_mems))
        if not parts:
            return ""
        return "\n\n【记忆上下文】以下为已生效的记忆（回答相关问题时引用，不确定时说明）：\n" + "\n\n".join(parts)
    except Exception as e:
        import logging

        logging.getLogger("agent.skill_router").warning("记忆注入失败: %s", str(e)[:100])
        return ""


async def run_skill_router(state: AgentState, config: RunnableConfig) -> dict:
    auto = state.get("auto_skill", False)
    skills = state.get("active_skills", []) or []
    role = state.get("user_role", "employee")
    dept_id = state.get("department_id", "")

    # 工具名集合（无关键词匹配：勾选集合或 auto 全量；run_script 兜底恒注入；
    # 4.1：skill 工具不再恒注入——功能栏单选启用（select_mode=single），auto 模式不包含）
    resolved = resolve_tool_names(skills, auto=auto, dept_id=dept_id)
    # 支柱 1（2026-08-10）：subagent 与 run_script 同级恒注入（能力面=只读+沙盒，无新增权限；
    # 受 subagent_enabled 配置控制——关闭即不注入）
    from app.core.config import get_settings as _get_settings

    always_inject = {"run_script", "intent_event", "result_event", "ask_user",
                     "zip_extract", "zip_pack", "skill_read",
                     "file_search"}  # v2：事件/反问工具恒注入（LLM 自主）；zip 工具 2026-08-21 恒注入（会话级基础能力）；skill_read 2026-08-26 恒注入（技能正文按需加载，只读）；file_search 2026-09-10 恒注入（找资料基础能力，只读，库级开关在工具内校验）
    if _get_settings().subagent_enabled:
        always_inject.add("subagent")
    if state.get("mode") == "complex":
        always_inject.add("todo_step")  # v2：复杂 P6 步骤清单（仅 complex）
    allowed_tools: set[str] = set(state.get("allowed_tools") or resolved) | always_inject
    # 2026-09-17：数据查询线（sql_query）已整线下线——工具本身不复存在，此处无需再判开关

    # 团队工具黑名单（2026-09-01 白名单→黑名单：勾选=禁用、未配置=全部可用——防漏配 403）；
    # 恒注入工具豁免（admin 勾选列表不含内部工具，防御性保留）；run_script 豁免（个人级沙盒，不归团队管控）；
    # 4.1 需求：黑名单覆盖全部默认技能（multi + 单选 skill）；团队AI技能（SKILL.md）已置灰不归运维管
    from app.services.config_service import get_dept_tools, get_user_tools

    dept_blocked = await get_dept_tools(dept_id)
    if dept_blocked is not None:
        allowed_tools = {t for t in allowed_tools if t not in dept_blocked
                         or t in ("run_script", "intent_event", "result_event", "todo_step", "ask_user",
                                  "zip_extract", "zip_pack", "skill_read")}
    # 用户级工具黑名单（2026-09-01：勾选=禁用）——与团队黑名单并集（任一禁用即禁用）；
    # run_script 纳入用户级控制：未配置用户级清单的用户仍恒注入（兜底不变），配置了清单的用户按清单过滤
    user_blocked = await get_user_tools(state.get("user_id") or 0)
    if user_blocked is not None:
        allowed_tools = {t for t in allowed_tools if t not in user_blocked
                         or t in ("intent_event", "result_event", "todo_step", "ask_user")}

    # MCP 外部工具双重闸门（2026-09-03）：① 运维 active（admin 在 mcp_tools 启停，≤60s 生效）
    # ② 员工个人显式启用（user_mcp.{uid}，McpPage 多选；未启用一律不注入——外部工具默认关）
    from app.agent.tools import get_mcp_tool_names, mcp_id_of
    from app.services.config_service import get_active_mcp_tool_ids, get_user_mcp_enabled

    extra_mcp: set[str] = set()
    mcp_names = get_mcp_tool_names()
    if mcp_names:
        active_mcp = await get_active_mcp_tool_ids()
        my_mcp = set(await get_user_mcp_enabled(state.get("user_id") or 0) or [])

        # 2026-09-16：一个 MCP 条目可对应多个工具（条目 id → 该条目的多个子工具），
        # 闸门按"归属条目"判而非工具名全等（mcp_id_of 兼容同名与 {id}_ 前缀两种）。
        def _allowed(t: str) -> bool:
            if t not in mcp_names:
                return True                      # 非外部工具：不受这道闸门约束
            mid = mcp_id_of(t, active_mcp | my_mcp)
            return bool(mid) and mid in active_mcp and mid in my_mcp

        allowed_tools = {t for t in allowed_tools if _allowed(t)}
        # 2026-09-17（走查 bug：/mcp 勾了外部工具、运维也 active，模型仍答"没有挂载 MCP 工具"）：
        # 上面那步只做**过滤**，而这些工具 hidden=True、不在 /skills 功能栏里，永远进不了
        # `resolved`（用户勾选集合）→ 等于**任何模式都注入不了**。/mcp 的勾选本就是它们的启用入口，
        # 所以双闸门通过即并入；团队/用户黑名单与其它工具同等生效（不能靠这道闸门绕开）。
        extra_mcp = {t for t in mcp_names if _allowed(t)}
        if dept_blocked is not None:
            extra_mcp = {t for t in extra_mcp if t not in dept_blocked}
        if user_blocked is not None:
            extra_mcp = {t for t in extra_mcp if t not in user_blocked}
        allowed_tools |= extra_mcp

    # 工具提示词（描述驱动）
    skills_prompt = build_skills_prompt(skills, auto=auto, dept_id=dept_id, extra_ids=extra_mcp)
    # 四期重构：团队技能（SKILL.md）description+正文常驻注入，失败不阻塞
    try:
        from app.services.skill_file_service import build_skill_files_prompt, list_active_skills

        # 2026-08-21：员工个人团队技能开关——注入按 团队active ∩ 员工启用 过滤（未配置=全启用）
        dept_skills = await list_active_skills(dept_id, state.get("user_id") or 0)
        if dept_skills:
            operator = f"user{state.get('user_id')}@{state.get('session_id', '')[:8]}"
            skills_prompt += ("\n\n" if skills_prompt else "") + await build_skill_files_prompt(
                dept_skills, dept_id, operator)
    except Exception as e:
        logger.warning("团队技能注入失败: %s", str(e)[:100])
    # 2026-08-21：全局技能（默认AI技能，运维发布）注入——三层交集（运维active ∩ 团队开关 ∩ 员工偏好）
    try:
        from app.services.skill_file_service import build_skill_files_prompt, list_active_global_skills

        global_skills = await list_active_global_skills(dept_id, state.get("user_id") or 0)
        if global_skills:
            operator = f"user{state.get('user_id')}@{state.get('session_id', '')[:8]}"
            skills_prompt += ("\n\n" if skills_prompt else "") + await build_skill_files_prompt(
                global_skills, "global", operator, section_title="全局技能", max_total_chars=1500)
    except Exception as e:
        logger.warning("全局技能注入失败: %s", str(e)[:100])

    # 四期缓存优化：system 静态化——记忆/文件等动态内容不进 system，
    # 组装 dynamic_context 随当前轮 user 消息注入（system 前缀稳定 → DeepSeek 缓存命中）
    memory_context = await _build_memory_context(state.get("user_id") or 0, dept_id)
    files_block = ""
    uploaded = state.get("uploaded_files") or []
    if uploaded:

        def _file_line(f: dict) -> str:
            line = f"- {f['file_name']}（类型 {f['file_type'] or '未知'}，服务器路径 {f['file_path']}）"
            if (f.get("file_type") or "") == "zip":
                line += _zip_unzip_block(f["file_path"])
            return line

        file_lines = "\n".join(_file_line(f) for f in uploaded)
        files_block = f"## 用户本次上传的文件\n{file_lines}\n需要读取或分析上传文件时，用对应工具并传入上面的服务器路径（file_path）。"
    # 归属三态（2026-08-24）：知识库表格文件块（新会话复制到会话可读目录——kb_service.copy_kb_xlsx_to_session；
    # 截断时附说明提示不全）
    kb_xlsx_block = ""
    kb_xlsx_files = state.get("kb_xlsx_files") or []
    if kb_xlsx_files:
        xlines = "\n".join(
            f"- {f['file_name']}（服务器路径 {f['file_path']}）" for f in kb_xlsx_files
        )
        suffix = "（知识库表格文件较多，本次只复制了部分）" if state.get("kb_xlsx_truncated") else ""
        kb_xlsx_block = ("## 知识库表格文件\n" + xlines + suffix
                         + "\n这是知识库中的表格文件，已复制到本次会话可读目录；需要时用 file_parse 读取（传上面的 file_path）。")
    # 4.6（2026-08-24）：数据库已更新提示块 —— 2026-09-17 随数据查询线下线删除
    # （它存在的唯一理由是"库更新后 sql_query 的旧结果作废"，来源 sync_log 也一并下线）
    # B10：历史产出记录注入（agent 跨轮可见自己生成的报告；只给文件名与 read_output 引导，不给服务器路径）
    outputs_block = ""
    prior = state.get("prior_outputs") or []
    if prior:
        outputs_block = ("## 本会话此前生成的产出\n" + "\n".join(str(x) for x in prior)
                         + "\n用户要求修改/引用此前生成的报告时，先用 read_output 读取原文件内容"
                           "（file_path 从产出记录获取），基于原文调整后重新调用 doc_export 生成新文件（保留原文件）。")
    # B7（D18）：最近工具过程摘要注入（跨轮追问"刚才查的数据"有据可依，不依赖 LLM 记住工具输出）
    tools_block = ""
    prior_tools = state.get("prior_tool_events") or []
    if prior_tools:
        lines = [ln for t in prior_tools for ln in (t.get("lines") or [])]
        if lines:
            tools_block = ("## 最近工具过程（此前轮次的查询/执行摘要）\n" + "\n".join(lines)
                           + "\n用户追问此前查询细节（如「刚才那 1000 行里 X 项是多少」）时，"
                             "优先依据上述摘要回答；摘要不足可重新查询或读取。")
    # B8（D20）：计划状态提示——confirmed 直接执行；rejected/expired 旧计划作废
    plan_block = ""
    plan_status = state.get("plan_status")
    if plan_status == "confirmed" and state.get("plan_confirmed"):
        plan_block = "【计划确认】用户已确认你的计划，直接按计划执行，不要再输出【计划】标记。"
    elif plan_status in ("rejected", "expired"):
        plan_block = "【计划已取消/过期】你之前的计划未获确认，不要按旧计划执行；如用户提出修改请重新输出完整计划等待确认。"
    # D32（2026-08-10）：本会话全部文件清单注入（未指定 file_ids 时 _load_uploaded_files 只带最近
    # 10 个——旧文件被挤掉后 Agent 无列表能力；此处补全量，Agent 可按需指定读取）
    all_files_block = ""
    try:
        import uuid as _uuid

        from sqlalchemy import select

        from app.core.database import get_global_engine
        from app.models import ChatFile

        sid = state.get("session_id", "")
        # B1（2026-08-10）：chat_files.session_id 为 uuid 列——非 UUID 会话 id（smoke/诊断测试）
        # 直接查询会 asyncpg DataError invalid input → 文件清单注入失败（08-10 smoke 三次噪音）；
        # 真实会话均为 UUID 不受影响，此处校验跳过避免日志噪音与异常路径
        try:
            _uuid.UUID(str(sid))
        except (ValueError, TypeError):
            pass  # 非 UUID 会话 id → 不注入文件清单（不是故障）
        else:
            async with get_global_engine().connect() as conn:
                frows = (
                    await conn.execute(
                        select(ChatFile.file_name, ChatFile.file_path, ChatFile.file_type)
                        .where(ChatFile.session_id == sid)
                        .order_by(ChatFile.created_at)
                    )
                ).all()
            if frows:
                flines = "\n".join(
                    f"- {r[0]}（类型 {r[2] or '未知'}，路径 {r[1]}）" + (_zip_unzip_block(r[1]) if r[2] == "zip" else "")
                    for r in frows
                )
                all_files_block = ("## 本会话全部上传文件清单（含早期文件）\n" + flines
                                   + "\n以上文件均可读取；引用时把 file_path 传给读取工具。")
    except Exception as e:
        logger.warning("文件清单注入失败 session=%s: %s", state.get("session_id", ""), repr(e)[:300])
    # 支柱 3a（2026-08-10）：任务清单注入（plan_status=confirmed 时从 plan_text 现场解析；
    # 清单块小且核心，不参与预算裁剪）
    _cfg = _get_settings()
    checklist_block = ""
    if _cfg.plan_checklist_enabled and plan_status == "confirmed" and state.get("plan_text"):
        from app.agent.plan_tasks import parse_plan_tasks

        tasks = parse_plan_tasks(state.get("plan_text") or "")
        if tasks:
            checklist_block = ("【任务清单】已确认计划共 %d 项，按序执行：\n" % len(tasks)
                               + "\n".join(f"- [ ] {i + 1}. {t}" for i, t in enumerate(tasks)))
    # 支柱 3b（2026-08-10）：dynamic_context 预算审计——超限按优先级整块丢弃
    # （确定性、不产生中间截断、前缀稳定）：记忆 → 工具摘要 → 文件清单（全量/本次上传）
    parts_spec = [
        (memory_context, "memory"),
        (files_block, "files"),
        (kb_xlsx_block, "kb_xlsx"),
        (outputs_block, "outputs"),
        (tools_block, "tools"),
        (plan_block, "plan"),
        (all_files_block, "all_files"),
        (checklist_block, "checklist"),
    ]
    # drop_order：越小越先丢（memory 最先）；kb_xlsx 在 all_files(2) 与 files(3) 之间
    drop_order = {"memory": 0, "tools": 1, "all_files": 2, "kb_xlsx": 2.5, "files": 3,
                  "outputs": 4, "plan": 5, "checklist": 6}
    budget = _cfg.dynamic_context_max_chars
    total = sum(len(b) for b, _ in parts_spec)
    dropped_labels: set[str] = set()
    if total > budget:
        for block, label in sorted(parts_spec, key=lambda x: drop_order[x[1]]):
            if total <= budget:
                break
            if block:
                dropped_labels.add(label)
                total -= len(block)
                logger.warning("dynamic_context 超预算裁剪 %s（预算 %d）", label, budget)
    dynamic_parts = [b for b, label in parts_spec if b and label not in dropped_labels]
    dynamic_context = ""
    if dynamic_parts:
        dynamic_context = "【上下文开始】\n" + "\n\n".join(dynamic_parts) + "\n【上下文结束】"

    # M2（2026-08-10）：轮次机制描述透传真实上限（system.py 内描述与 graph 硬兜底一致）；
    # v2：mode 透传（双模式语义段；会话级恒定保缓存前缀）
    system_prompt = build_system_prompt(
        role=role,
        dept_id=dept_id,
        dept_name=await get_dept_name(dept_id),
        skills_prompt=skills_prompt,
        max_tool_rounds=_get_settings().max_tool_rounds,
        mode=state.get("mode") or "quick",
    )
    # 支柱 3a：任务清单写入 state（agent_llm 逐轮进度回显用；无确认执行轮为空）
    plan_tasks: list[str] = []
    if checklist_block:
        from app.agent.plan_tasks import parse_plan_tasks

        plan_tasks = parse_plan_tasks(state.get("plan_text") or "")
    return {"system_prompt": system_prompt, "allowed_tools": sorted(allowed_tools),
            "dynamic_context": dynamic_context, "plan_tasks": plan_tasks}
