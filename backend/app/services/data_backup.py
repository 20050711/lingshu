"""平台数据导出/导入（admin 专属，三期）：全量 JSON 备份与一键覆盖恢复。

- export_all:  全局库全部表 → dict
- import_all:  导入前 pg_dump 备份全局库 → TRUNCATE CASCADE + INSERT 覆盖 → 统计

2026-09-17（数据查询线下线）：**不再遍历团队库**——dept_*_db 已无业务写入，
原实现按"每个团队库全表"遍历且无 try，团队库一删/失联整页 500（用户决策：保留本页并瘦身）。
旧导出文件里的 depts 段在导入时被忽略。

序列化约定（HANDOVER 踩坑 3/4/12）：
- datetime → isoformat（naive，导入时 fromisoformat 还原为 naive datetime）
- JSONB dict/list → 导出原样（外层 json.dumps）；导入时 json.dumps 字符串（原生 SQL 写 JSONB）
- 导入用 TRUNCATE ... CASCADE（忽略 FK 顺序，父子表一并清空）
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine

_settings = get_settings()

PLATFORM_TAG = "ai-platform"


def _serialize(value):
    if isinstance(value, datetime):
        # 加 "Z" 标记区分 datetime 与 TEXT 列里恰好是 ISO 格式的字符串（导入时仅识别带标记的）
        return value.isoformat(timespec="microseconds") + "Z"
    return value  # dict/list（JSONB）原样保留


def _import_value(value):
    """导入值还原：JSONB 列 → json.dumps 字符串（原生 SQL）；"Z" 结尾的 ISO 字符串 → naive datetime。"""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.endswith("Z"):
        try:
            return datetime.fromisoformat(value[:-1])
        except ValueError:
            return value
    return value


async def _list_tables(engine) -> list[str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT table_name FROM information_schema.tables "
                     "WHERE table_schema='public' ORDER BY table_name")
            )
        ).all()
    return [r[0] for r in rows]


async def _table_rows(engine, table: str) -> list[dict]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text(f'SELECT * FROM "{table}"'))).all()
    return [{k: _serialize(v) for k, v in r._mapping.items()} for r in rows]


async def export_all() -> dict:
    """导出平台全量数据（仅全局库）。"""
    data = {
        "platform": PLATFORM_TAG,
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "global": {},
        "depts": {},   # 保留空键：与旧导出文件格式兼容（导入侧忽略该段）
    }
    g = get_global_engine()
    for t in await _list_tables(g):
        data["global"][t] = await _table_rows(g, t)
    return data


async def _dump_before_import() -> list[str]:
    """导入前备份将被覆盖的全局库（pg_dump），返回备份文件名。"""
    from app.core.config import get_settings
    from app.services.backup import dump_database

    # 2026-09-28（tardis_ai 副本）：库名从配置 URL 取（原硬编码 ai_platform_tardis 会备到公司库）
    db_name = get_settings().global_db_url.rsplit("/", 1)[-1]
    f = await dump_database("global", db_name=db_name)
    return [f.name] if f else []


# 全局库插入顺序（FK 依赖拓扑序：父表先于子表；表名字母序不可靠——ceo_memory 曾在 users 前插入违反 FK）
# 追加表（如未来新增）自动排在最后（容错；若存在 FK 依赖需在此补充）
_GLOBAL_INSERT_ORDER = [
    "users", "kb_categories", "departments", "system_config", "skill_configs", "mcp_tools",
    "audit_log", "security_events", "feedback", "memory_thresholds",
    "department_memory", "resume_batches",
    "sessions", "kb_documents", "user_memory", "user_skill_prefs", "skill_files", "resume_items",
    "chat_messages", "chart_outputs", "chat_files",
]


def _ordered_tables(tables: set[str]) -> list[str]:
    ordered = [t for t in _GLOBAL_INSERT_ORDER if t in tables]
    ordered += [t for t in sorted(tables) if t not in ordered]
    return ordered


async def _overwrite_global(payload: dict) -> dict[str, int]:
    g = get_global_engine()
    tables = _ordered_tables(set(payload.get("global", {}).keys()))
    stats: dict[str, int] = {}
    async with g.begin() as conn:
        if tables:
            await conn.execute(text(f'TRUNCATE TABLE {", ".join(tables)} CASCADE'))
        for t in tables:
            rows = payload["global"][t]
            # 自引用表（kb_categories.parent_id）按 id 升序插入，保证父行先于子行
            if t == "kb_categories":
                rows = sorted(rows, key=lambda r: r.get("id") or 0)
            for r in rows:
                cols = list(r.keys())
                placeholders = ", ".join(f":{c}" for c in cols)
                await conn.execute(
                    text(f'INSERT INTO "{t}" ({", ".join(cols)}) VALUES ({placeholders})'),
                    {c: _import_value(v) for c, v in r.items()},
                )
            stats[t] = len(rows)
    return stats


async def import_all(payload: dict) -> dict:
    """一键覆盖导入：校验 → 备份 → 覆盖全局库与团队库 → 返回统计。"""
    if not isinstance(payload, dict) or payload.get("platform") != PLATFORM_TAG:
        raise ValueError("文件不是本平台导出的数据（platform 标识不符）")

    # 兼容旧备份键名（四期重构 ceo_memory → user_memory）
    g = payload.get("global") or {}
    if "ceo_memory" in g and "user_memory" not in g:
        g["user_memory"] = g.pop("ceo_memory")

    backup_files = await _dump_before_import()

    # 2026-09-17：不再覆盖团队库（payload 里的 depts 段忽略）
    stats = {"global": await _overwrite_global(payload), "depts": {}}
    stats["backup_files"] = backup_files
    return stats
