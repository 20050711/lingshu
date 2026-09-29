"""记忆库工具（四期重构：memory 单工具参数驱动）。

- scope=personal（默认）：个人记忆（全员可用，按 user_id 隔离，立即生效，无共识）
- scope=dept：团队共识记忆（候选-激活机制，显式"记住"立即 active）
Agent 消费：skill_router 自动注入记忆进 system_prompt；
显式查询（"显示我的记忆库"）由模型调用 memory action=list 主动取。
"""
from __future__ import annotations

from app.agent.tools import ToolContext, ToolSpec, register_tool
from app.services.memory_service import get_active_for_dept, record_memory, user_add, user_delete, user_list


async def run_memory(args: dict, ctx: ToolContext) -> dict:
    action = str(args.get("action") or "add")
    scope = str(args.get("scope") or "personal")

    # ---- list ----
    if action == "list":
        if scope == "dept":
            mems = await get_active_for_dept(ctx.department_id, limit=10)
            return {"memories": [{"category": m["category"], "content": m["content"]} for m in mems]}
        if not ctx.user_id:
            return {"error": "无法识别当前用户"}
        mems = await user_list(ctx.user_id)
        return {"memories": [{"id": m["id"], "type": m["mem_type"], "content": m["content"]} for m in mems]}

    # ---- delete ----
    if action == "delete":
        # E-01（数据层附加）：LLM 传非数字 memory_id 不再抛异常（异常会被队列误判为 Redis 故障）
        try:
            mem_id = int(args.get("memory_id") or 0)
        except (TypeError, ValueError):
            mem_id = 0
        if not mem_id:
            return {"error": "需要提供 memory_id"}
        if scope == "dept":
            return {"error": "团队记忆由管理员管理，员工可表达新偏好覆盖"}
        if not ctx.user_id:
            return {"error": "无法识别当前用户"}
        ok = await user_delete(ctx.user_id, mem_id)
        return {"deleted": ok, "note": "已删除" if ok else "记忆不存在"}

    # ---- add（默认）----
    content = str(args.get("content", "")).strip()
    if not content:
        return {"error": "需要提供记忆内容"}
    if scope == "personal":
        if not ctx.user_id:
            return {"error": "无法识别当前用户"}
        mem_type = str(args.get("mem_type") or "knowledge")
        if mem_type not in ("profile", "knowledge", "workflow"):
            mem_type = "knowledge"
        mem_id = await user_add(ctx.user_id, mem_type, content)
        return {"memory_id": mem_id, "status": "active", "note": "已保存到个人记忆（立即生效）"}
    explicit = bool(args.get("explicit", False))
    category = str(args.get("category") or "general")[:50]
    try:
        mem = await record_memory(ctx.department_id, content, ctx.client_id or "unknown", category, explicit)
    except ValueError as e:  # SEC-08：注入内容拒绝入库，转工具 error（LLM 可见原因）
        return {"error": str(e)}
    note = "已记住（立即生效）" if mem["status"] == "active" else "已记录为候选（多轮共识后自动生效）"
    return {"memory_id": mem["id"], "status": mem["status"], "note": note}


register_tool(
    ToolSpec(
        name="memory", progress_keys=("memories", "note", "deleted", "memory_id"),
        write=True,
        display_name="记忆库",
        icon="brain",
        summary="记住偏好、查看或删除个人/团队记忆",
        group="记忆",
        sort_order=9,
        user_description=(
            "管理 AI 助理的记忆：记住你的偏好与习惯（个人记忆，立即生效）、查看或删除已记住的内容；"
            "团队共识记忆需显式确认后生效。"
        ),
        description=(
            "What：管理记忆——保存（add）/查看（list）/删除（delete）个人或团队记忆。\n"
            "When：用户说「记住/以后都/我的偏好是」→ add（记一句话偏好或业务知识）；"
            "「显示我的记忆/记住了什么」→ list；「忘掉那条记忆」→ delete。\n"
            "How：action 必填；scope=personal 个人记忆（本人可见立即生效，mem_type=profile/knowledge/workflow 区分类型）；"
            "scope=dept 团队共识（explicit=true 立即生效，否则为候选多轮共识后生效）；delete 传 memory_id（来自 list）。\n"
            "Result：返回操作结果（已记住/候选/列表/删除）；列表回答向用户简述记忆内容。\n"
            "**边界：记忆读写失败不要反复重试；查不到相关内容就如实说「没有相关记忆」，不要编造记忆内容。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "delete"], "description": "操作（默认 add）"},
                "scope": {"type": "string", "enum": ["personal", "dept"], "description": "记忆范围（默认 personal）"},
                "content": {"type": "string", "description": "要记住的内容（add 必填，一句话）"},
                "explicit": {"type": "boolean", "description": "团队记忆是否明确要求记住（true=立即生效）"},
                "category": {"type": "string", "description": "团队记忆类别（默认 general）"},
                "mem_type": {"type": "string", "enum": ["profile", "knowledge", "workflow"], "description": "个人记忆类型"},
                "memory_id": {"type": "integer", "description": "要删除的记忆 id（delete 必填，来自 list 结果）"},
            },
            "required": ["action"],
        },
        queue="default",
        handler=run_memory,
    )
)
