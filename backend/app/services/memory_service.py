"""记忆库服务（二期 M9，四期重构为团队记忆库 + 全员个人记忆库）。

- 团队共识记忆：候选-激活机制。候选只来自 LLM 触发的 memory_add 工具（不做消息文本自动挖掘）；
  scan_candidates 每小时执行共识判定/替换/过期清理。
- 个人记忆（user_memory，原 ceo_memory 泛化）：全员可用，按 user_id 隔离，
  三类（profile/knowledge/workflow），显式保存立即生效、无共识机制。CRUD + JSON 导出导入。
- Agent 消费：get_active_for_dept / get_user_memories 注入 system_prompt（skill_router 调用）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import text

from app.core.database import get_global_engine
from app.core.logging import get_logger

logger = get_logger("services.memory_service")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


async def _get_thresholds() -> dict:
    engine = get_global_engine()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM memory_thresholds WHERE id=1")
            )
        ).first()
    if row is None:
        return {"breadth_min_clients": 3, "strength_min_rounds": 5, "strength_min_clients": 2,
                "window_days": 7, "candidate_ttl_days": 30, "max_active_per_dept": 50}
    return {
        "breadth_min_clients": row.breadth_min_clients, "strength_min_rounds": row.strength_min_rounds,
        "strength_min_clients": row.strength_min_clients, "window_days": row.window_days,
        "candidate_ttl_days": row.candidate_ttl_days, "max_active_per_dept": row.max_active_per_dept,
    }


# ---------------------------------------------------------------- 写入（memory_add 工具调用）

async def record_memory(dept_id: str, content: str, client_id: str, category: str = "general",
                        explicit: bool = False) -> dict:
    """记录一条团队记忆。

    - explicit=True（用户明确"记住"）→ 立即激活
    - 否则作为候选累积：同 content_hash 已存在则追加 client_id/round_count

    SEC-08（2026-08-17）：写入端注入检测——记忆激活后全员注入 system 上下文，
    含注入内容（忽略指令/要求密钥等）拒绝入库（ValueError 由调用方转工具 error）。
    """
    content = content.strip()
    from app.core.input_filter import InputFilter

    filt = InputFilter.check(content, dept_id)
    if not filt["passed"]:
        raise ValueError(f"记忆内容未保存：检测到疑似注入/越权内容（{filt['reason']}）")
    h = content_hash(content)
    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT id, status, client_ids, round_count FROM department_memory "
                     "WHERE dept_id=:d AND content_hash=:h AND status != 'replaced' ORDER BY id LIMIT 1"),
                {"d": dept_id, "h": h},
            )
        ).first()
        if row:
            clients = (row.client_ids or []) if isinstance(row.client_ids, list) else json.loads(row.client_ids or "[]")
            if client_id not in clients:
                clients.append(client_id)
            new_status = "active" if (explicit or row.status == "active") else "candidate"
            await conn.execute(
                text("UPDATE department_memory SET client_ids=:c, round_count=round_count+1, "
                     "status=:s, last_seen_at=NOW(), updated_at=NOW() WHERE id=:id"),
                {"c": json.dumps(clients, ensure_ascii=False), "s": new_status, "id": row.id},
            )
            if explicit and row.status != "active":
                await conn.execute(
                    text("UPDATE department_memory SET activated_at=NOW() WHERE id=:id"), {"id": row.id}
                )
            return {"id": row.id, "status": new_status, "explicit": explicit}
        clients = [client_id]
        status = "active" if explicit else "candidate"
        mem_id = (
            await conn.execute(
                text("INSERT INTO department_memory (dept_id, category, content, content_hash, "
                     "client_ids, round_count, status, activated_at) "
                     "VALUES (:d, :c, :ct, :h, :cl, 1, :s, :a) RETURNING id"),
                {"d": dept_id, "c": category, "ct": content, "h": h,
                 "cl": json.dumps(clients, ensure_ascii=False), "s": status,
                 "a": datetime.now() if explicit else None},
            )
        ).scalar()
    return {"id": mem_id, "status": status, "explicit": explicit}


# ---------------------------------------------------------------- 共识扫描（每小时）

async def scan_candidates() -> dict:
    """候选激活判定 + 过期清理 + 替换旧记忆。返回统计（供日志/验收）。"""
    th = await _get_thresholds()
    engine = get_global_engine()
    now = datetime.now()
    stats = {"activated": 0, "replaced": 0, "deleted": 0}

    async with engine.connect() as conn:
        candidates = (
            await conn.execute(
                text("SELECT id, dept_id, category, content, client_ids, round_count, "
                     "first_seen_at, last_seen_at "
                     "FROM department_memory WHERE status='candidate'")
            )
        ).all()

    for c in candidates:
        clients = c.client_ids if isinstance(c.client_ids, list) else json.loads(c.client_ids or "[]")
        n_clients = len(set(clients))
        last_seen = c.last_seen_at

        # 过期（D24，2026-08-10：判定改 first_seen_at）——原按 last_seen_at 时被反复提及但
        # 达不到共识阈值的候选每 7 天刷新 last_seen → 永不淘汰；first_seen 是真实存活时长
        if c.first_seen_at < now - timedelta(days=th["candidate_ttl_days"]):
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM department_memory WHERE id=:id"), {"id": c.id})
                await conn.execute(
                    text("INSERT INTO audit_log (operator, action, target, detail) "
                         "VALUES ('system', 'memory.delete', :t, :d)"),
                    {"t": c.content[:80], "d": json.dumps({"dept_id": c.dept_id, "reason": "expired"})},
                )
            stats["deleted"] += 1
            continue

        # 激活判定（7 天窗口内）
        in_window = last_seen >= now - timedelta(days=th["window_days"])
        breadth_ok = n_clients >= th["breadth_min_clients"]
        strength_ok = c.round_count >= th["strength_min_rounds"] and n_clients >= th["strength_min_clients"]
        if not (in_window and (breadth_ok or strength_ok)):
            continue

        # 激活：同 category 已有 active 旧记忆 → 置 replaced
        async with engine.begin() as conn:
            old = (
                await conn.execute(
                    text("SELECT id FROM department_memory WHERE dept_id=:d AND category=:c AND status='active' "
                         "AND id != :id LIMIT 1"),
                    {"d": c.dept_id, "c": c.category, "id": c.id},
                )
            ).first()
            if old:
                await conn.execute(
                    text("UPDATE department_memory SET status='replaced', updated_at=NOW() WHERE id=:id"),
                    {"id": old.id},
                )
                await conn.execute(
                    text("INSERT INTO audit_log (operator, action, target, detail) "
                         "VALUES ('system', 'memory.replace', :t, :d)"),
                    {"t": c.content[:80], "d": json.dumps({"dept_id": c.dept_id, "old_id": old.id, "new_id": c.id})},
                )
                stats["replaced"] += 1
            # D24（2026-08-10）：激活前检查该团队 active 总数 >= max_active_per_dept →
            # 淘汰 activated_at 最旧的（置 replaced）——原 max_active_per_dept 是死配置无任何消费
            active_count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM department_memory WHERE dept_id=:d AND status='active'"),
                    {"d": c.dept_id},
                )
            ).scalar()
            if active_count >= th["max_active_per_dept"]:
                oldest = (
                    await conn.execute(
                        text("SELECT id FROM department_memory WHERE dept_id=:d AND status='active' "
                             "ORDER BY activated_at ASC NULLS LAST, id ASC LIMIT 1"),
                        {"d": c.dept_id},
                    )
                ).first()
                if oldest:
                    await conn.execute(
                        text("UPDATE department_memory SET status='replaced', updated_at=NOW() WHERE id=:id"),
                        {"id": oldest.id},
                    )
                    stats["replaced"] += 1
            await conn.execute(
                text("UPDATE department_memory SET status='active', activated_at=NOW(), updated_at=NOW() WHERE id=:id"),
                {"id": c.id},
            )
            await conn.execute(
                text("INSERT INTO audit_log (operator, action, target, detail) "
                     "VALUES ('system', 'memory.promote', :t, :d)"),
                {"t": c.content[:80], "d": json.dumps({"dept_id": c.dept_id, "n_clients": n_clients, "rounds": c.round_count})},
            )
        stats["activated"] += 1

    logger.info("记忆库扫描完成: %s", stats)
    return stats


# ---------------------------------------------------------------- Agent 消费

async def get_active_for_dept(dept_id: str, limit: int = 5) -> list[dict]:
    """团队 active 记忆（最近激活优先，上限默认 5 条）。"""
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT category, content FROM department_memory "
                     "WHERE dept_id=:d AND status='active' ORDER BY activated_at DESC LIMIT :n"),
                {"d": dept_id, "n": limit},
            )
        ).all()
    return [{"category": r.category, "content": r.content} for r in rows]


async def get_user_memories(user_id: int, per_type: int = 2) -> list[dict]:
    """个人记忆（四期重构：全员个人记忆；每类 ≤per_type 条，最近更新优先）。"""
    if not user_id:
        return []
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT mem_type, content FROM user_memory WHERE user_id=:a "
                     "ORDER BY updated_at DESC LIMIT :n"),
                {"a": user_id, "n": per_type * 3},
            )
        ).all()
    result: list[dict] = []
    seen: dict[str, int] = {}
    for r in rows:
        if seen.get(r.mem_type, 0) >= per_type:
            continue
        seen[r.mem_type] = seen.get(r.mem_type, 0) + 1
        result.append({"type": r.mem_type, "content": r.content})
    return result


async def user_add(user_id: int, mem_type: str, content: str) -> int:
    engine = get_global_engine()
    async with engine.begin() as conn:
        mem_id = (
            await conn.execute(
                text("INSERT INTO user_memory (user_id, mem_type, content) VALUES (:a, :t, :c) RETURNING id"),
                {"a": user_id, "t": mem_type, "c": content.strip()},
            )
        ).scalar()
    return mem_id


async def user_list(user_id: int) -> list[dict]:
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT id, mem_type, content, created_at FROM user_memory WHERE user_id=:a ORDER BY updated_at DESC"),
                {"a": user_id},
            )
        ).all()
    return [{"id": r.id, "mem_type": r.mem_type, "content": r.content,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


async def user_delete(user_id: int, mem_id: int) -> bool:
    engine = get_global_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text("DELETE FROM user_memory WHERE id=:id AND user_id=:a"), {"id": mem_id, "a": user_id}
        )
    return result.rowcount > 0


# ---------------------------------------------------------------- 导出/导入（管理后台备份）

async def export_all() -> dict:
    engine = get_global_engine()
    async with engine.connect() as conn:
        dept_rows = (
            await conn.execute(
                text("SELECT id, dept_id, category, content, status, client_ids, round_count, "
                     "first_seen_at, last_seen_at, activated_at FROM department_memory ORDER BY id")
            )
        ).all()
        user_rows = (
            await conn.execute(
                text("SELECT id, user_id, mem_type, content FROM user_memory ORDER BY id")
            )
        ).all()
    return {
        "version": 1,
        "department_memory": [
            {"dept_id": r.dept_id, "category": r.category, "content": r.content, "status": r.status,
             "client_ids": r.client_ids, "round_count": r.round_count,
             "first_seen_at": r.first_seen_at.isoformat() if r.first_seen_at else None,
             "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
             "activated_at": r.activated_at.isoformat() if r.activated_at else None}
            for r in dept_rows
        ],
        "user_memory": [{"user_id": r.user_id, "mem_type": r.mem_type, "content": r.content} for r in user_rows],
    }


async def import_all(data: dict) -> dict:
    engine = get_global_engine()
    imported = {"dept": 0, "user": 0}
    async with engine.begin() as conn:
        for r in data.get("department_memory") or []:
            await conn.execute(
                text("INSERT INTO department_memory (dept_id, category, content, content_hash, status, "
                     "client_ids, round_count, first_seen_at, last_seen_at, activated_at) "
                     "VALUES (:d, :c, :ct, :h, :s, :cl, :rc, :f, :l, :a)"),
                {"d": r["dept_id"], "c": r.get("category", "general"), "ct": r["content"],
                 "h": content_hash(r["content"]), "s": r.get("status", "candidate"),
                 "cl": json.dumps(r.get("client_ids") or [], ensure_ascii=False), "rc": r.get("round_count", 1),
                 "f": r.get("first_seen_at") or datetime.now().isoformat(),
                 "l": r.get("last_seen_at") or datetime.now().isoformat(),
                 "a": r.get("activated_at")},
            )
            imported["dept"] += 1
        # 兼容旧备份（ceo_memory 键名）
        for r in data.get("user_memory") or data.get("ceo_memory") or []:
            await conn.execute(
                text("INSERT INTO user_memory (user_id, mem_type, content) VALUES (:a, :t, :c)"),
                {"a": r.get("user_id") or r.get("account_id"), "t": r.get("mem_type", "knowledge"), "c": r["content"]},
            )
            imported["user"] += 1
    return imported
