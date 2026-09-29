"""记忆自动提取（2026-08-05 用户需求）：会话空闲 N 小时后，从对话中自动提炼记忆。

触发：单会话最后消息距今 > memory_extract_idle_hours（默认 2 小时）且未提取过（sessions.memory_extracted_at）。
流程：取会话消息 → LLM 按提取规范提炼（含与已有个人记忆的语义去重）→ 写 user_memory（个人记忆）→ 标记已提取。
归属：写入个人记忆（自动提取属个人沉淀；团队共识仍走显式 memory 工具/候选机制）。
安全：提取失败静默不阻塞；memory_extract_enabled=false 一键关闭；只处理本会话所属用户的消息。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine
from app.core.logging import get_logger
from app.services.memory_service import user_add

logger = get_logger("services.memory_extract")
_settings = get_settings()

_MAX_ROUNDS = 20       # 单会话最多取 20 轮消息（控制 token）
_MAX_NEW_MEMORIES = 3  # 单会话最多新增 3 条（防滥提）

_EXTRACT_PROMPT = """你是企业的记忆提取助手。请从下面的会话对话中，提取**值得长期记住**的用户信息，输出 JSON 数组。

## 提取标准（满足任一即可提取）
1. 用户明确表达的偏好/习惯（如「我喜欢柱状图」「以后都用 PDF 报告」）
2. 多轮一致出现的工作模式/约定
3. 重要的业务知识（术语定义、数据口径、专有名词解释）
4. 用户自述的工作身份/职责范围

## 不提取
1. 一次性任务请求（「帮我查下 X」「画个图」）
2. 闲聊寒暄、情绪化表达
3. 敏感信息（密码、密钥、token、身份证号、手机号等）
4. 与工作无关的个人隐私

## 去重规则（重要）
下面会给出该用户**已有记忆**。候选内容与已有记忆**含义近似**（表达不同但说的是一件事）视为重复，跳过不输出。

## 输出格式
严格输出 JSON 数组（不要其他文字）：
[{{"mem_type": "profile", "content": "一句话"}}, ...]
- mem_type: profile=偏好/习惯/身份；knowledge=业务知识/术语/口径
- content 用一句话概括，≤50 字
- 最多 {max_new} 条；没有值得提取的输出 []

## 会话对话
{conversation}

## 已有记忆
{existing}
"""


def _parse_memories(text_content: str) -> list[dict]:
    """从 LLM 输出中解析 JSON 数组（容忍 ```json 围栏与前后杂质）。"""
    m = re.search(r"\[.*\]", text_content, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict) and d.get("content"):
            mt = d.get("mem_type") if d.get("mem_type") in ("profile", "knowledge") else "knowledge"
            out.append({"mem_type": mt, "content": str(d["content"]).strip()[:50]})
    return out[: _MAX_NEW_MEMORIES]


async def _load_session_messages(session_id: str) -> list[dict]:
    """E-07(数据层，2026-08-10)：取**最近** _MAX_ROUNDS×2 条消息（原 ORDER BY ASC LIMIT 取的是
    会话开头 40 条——长会话提取喂给 LLM 的是寒暄/首次请求，最近的偏好表达完全不在输入中）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT role, content FROM chat_messages WHERE session_id=:s "
                     "ORDER BY created_at DESC LIMIT :n"),
                {"s": session_id, "n": _MAX_ROUNDS * 2},
            )
        ).all()
    return [{"role": r.role, "content": r.content} for r in reversed(rows) if r.content]


async def _load_existing(user_id: int) -> list[str]:
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT content FROM user_memory WHERE user_id=:u ORDER BY updated_at DESC LIMIT 50"),
                {"u": user_id},
            )
        ).all()
    return [r[0] for r in rows]


async def extract_from_session(session_id: str, user_id: int) -> list[dict]:
    """提取单个会话的记忆（提取 + 语义去重 + 入库 + 标记）。返回新增记忆列表；失败静默。"""
    try:
        from app.agent.llm_client import DeepSeekLLM

        messages = await _load_session_messages(session_id)
        if not messages:
            return []
        existing = await _load_existing(user_id)
        conv = "\n".join(f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:800]}" for m in messages[-_MAX_ROUNDS * 2:])
        existing_text = "\n".join(f"- {c}" for c in existing) or "（无）"
        prompt = _EXTRACT_PROMPT.format(max_new=_MAX_NEW_MEMORIES, conversation=conv, existing=existing_text)

        # LLM 辅助任务档（llm_aux，按用户团队/角色分层，任务=记忆提取）；未配置回退 employee 主模型
        from app.services.config_service import get_aux_model_cfg_for_user

        aux = await get_aux_model_cfg_for_user(user_id, "memory_extract")
        llm = DeepSeekLLM(role="employee",
                          model=aux["model"] if aux and aux.get("model") else None,
                          effort=aux.get("effort") if aux else None,
                          platform=str(aux.get("platform") or "deepseek") if aux else None,
                          thinking=aux.get("thinking") if aux else None)  # 2026-08-12 思考三选项透传
        msg = await llm.ainvoke([{"role": "user", "content": prompt}])
        candidates = _parse_memories(msg.get("content") or "")

        added = []
        for cand in candidates:
            await user_add(user_id, cand["mem_type"], cand["content"])
            added.append(cand)
        engine = get_global_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE sessions SET memory_extracted_at=NOW() WHERE id=:s"), {"s": session_id}
            )
        logger.info("记忆提取 session=%s user=%s 新增=%d", str(session_id)[:8], user_id, len(added))
        return added
    except Exception as e:
        logger.warning("记忆提取失败 session=%s err=%s", str(session_id)[:8], str(e)[:150])
        return []


async def scan_idle_sessions() -> dict:
    """扫描空闲（> idle_hours 未活动）且待提取的会话并提取。返回统计。

    D12（2026-08-10）：不再一次性抑制——未提取过 **或** 上次提取后新增消息 >= 20 条
    （长期会话的后续新消息可持续被提取；memory_extracted_at 语义变为"最近提取时间"）。
    """
    if not _settings.memory_extract_enabled:
        return {"enabled": False, "extracted": 0, "skipped": 0}
    cutoff = datetime.now() - timedelta(hours=_settings.memory_extract_idle_hours)
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT s.id, s.user_id, s.memory_extracted_at, "
                    "  (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id "
                    "   AND (s.memory_extracted_at IS NULL OR m.created_at > s.memory_extracted_at)) AS new_msgs "
                    "FROM sessions s "
                    "WHERE s.last_activity_at < :c "
                    "  AND (s.memory_extracted_at IS NULL OR "
                    "       (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id "
                    "        AND m.created_at > s.memory_extracted_at) >= :min_new) "
                    "ORDER BY s.last_activity_at ASC LIMIT 20"),
                {"c": cutoff, "min_new": _settings.memory_extract_min_new_messages},
            )
        ).all()
    stats = {"enabled": True, "extracted": 0, "skipped": 0}
    for sid, uid, _extracted_at, _new_msgs in rows:
        if _settings.memory_extract_idle_hours > 0:
            added = await extract_from_session(str(sid), uid)
            stats["extracted"] += 1 if added else 0
            stats["skipped"] += 1 if not added else 0
    if rows:
        logger.info("记忆提取扫描完成: 处理 %d 个会话 %s", len(rows), stats)
    return stats
