"""团队服务（三期 M12 多团队正式化）。

- create_dept：注册团队 = departments 表 + CREATE DATABASE dept_{id}_db（幂等）
- get_dept_name：团队中文名（60s 进程内缓存，替代 DEPT_NAMES 硬编码，供 auth/skill_router/admin）
- list_depts / dept_stats：团队列表与各库状态（信息表数/总行数）
"""
from __future__ import annotations

import re
import time

import asyncpg
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import get_global_engine

_settings = get_settings()

DEPT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,29}$")

# 团队名缓存（60s 失效：新团队注册后 ≤60s 生效，无需重启）
_NAME_CACHE: dict = {"ts": 0.0, "names": {}}
_CACHE_TTL = 60


async def _load_names() -> dict[str, str]:
    """查 departments 表全部 dept_id→name 映射（带 60s 缓存）。"""
    now = time.time()
    if now - _NAME_CACHE["ts"] < _CACHE_TTL:
        return _NAME_CACHE["names"]
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT dept_id, name FROM departments"))).all()
    _NAME_CACHE["ts"] = now
    _NAME_CACHE["names"] = {r[0]: r[1] for r in rows}
    return _NAME_CACHE["names"]


async def get_dept_name(dept_id: str) -> str:
    """团队中文名；未注册团队回退 dept_id 本身（兼容历史数据）。"""
    names = await _load_names()
    return names.get(dept_id, dept_id)


def _admin_db_conn_url() -> dict:
    """解析 global_db_url 的 host/port/user/password（建库连接 postgres 库用）。"""
    url = _settings.global_db_url  # postgresql+asyncpg://user:pass@host:port/db
    rest = url.split("//", 1)[1]
    cred, hostpart = rest.split("@", 1)
    user, password = cred.split(":", 1)
    host, port = hostpart.split(":", 1)
    port = port.split("/", 1)[0]
    return {"host": host, "port": int(port), "user": user, "password": password}


async def create_dept(dept_id: str, name: str) -> str:
    """注册团队：写 departments 表（幂等）+ 创建 dept_{id}_db（幂等跳过）。

    团队已存在时仅确保库存在（补建）；CREATE DATABASE 不能在事务/连接池连接中执行，
    须用 asyncpg autocommit 独立连接。
    """
    if not DEPT_ID_RE.match(dept_id):
        raise ValueError("团队标识须为小写字母开头的 2-30 位小写字母/数字/下划线")
    if not name or len(name) > 100:
        raise ValueError("团队名称不能为空且不超过 100 字")
    # M4（2026-08-12，双端防御）：平台团队不创建 dept_{id}_db 业务库——ceo 实际用 tardis_ceo_db，
    # dept_root 无业务库；照建会产生无人消费的孤儿库
    if dept_id in ("ceo", "dept_root"):
        raise ValueError(f"平台团队 {dept_id} 不创建业务库（ceo 使用 tardis_ceo_db；dept_root 无业务库）")
    # 2026-08-21：'global' 为全局技能（默认AI技能）dept_id 哨兵值，禁止用作真实团队
    if dept_id == "global":
        raise ValueError("global 为保留标识（全局技能作用域），不能作为团队标识")

    engine = get_global_engine()
    async with engine.begin() as conn:
        exists = (await conn.execute(text("SELECT 1 FROM departments WHERE dept_id=:d"), {"d": dept_id})).first()
        if not exists:
            # ON CONFLICT 防并发注册同 id 的 IntegrityError（多 admin 并发场景）
            await conn.execute(
                text("INSERT INTO departments (dept_id, name) VALUES (:d, :n) ON CONFLICT (dept_id) DO NOTHING"),
                {"d": dept_id, "n": name},
            )

    # 建库（幂等：库已存在跳过；asyncpg 简单查询自动提交，可执行 CREATE DATABASE）
    conn_kw = _admin_db_conn_url()
    created = False
    try:
        conn = await asyncpg.connect(database="postgres", **conn_kw)
        try:
            await conn.execute(f'CREATE DATABASE "{_settings.db_name_prefix}tardis_dept_{dept_id}_db"')
            created = True
        except asyncpg.DuplicateDatabaseError:
            pass
        finally:
            await conn.close()
    except (OSError, asyncpg.PostgresError) as e:
        # M13：网络/认证/库名异常（asyncpg.PostgresError 含 InvalidPasswordError/InvalidCatalogNameError）：
        # 若本次刚插入团队记录则回滚，避免 departments 有记录但库不存在（孤儿团队行）
        if not exists:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM departments WHERE dept_id=:d"), {"d": dept_id})
        raise ValueError(f"创建团队库失败: {str(e)[:200]}") from e
    _NAME_CACHE["ts"] = 0  # 失效缓存，立即可见新团队名
    return "created" if (created or not exists) else "exists"


# 2026-09-17：ensure_personal_db 随数据查询线下线删除（个人库不再由导入创建；存量库原地保留）


async def list_depts() -> list[dict]:
    engine = get_global_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT dept_id, name, status, created_at FROM departments ORDER BY dept_id")
            )
        ).all()
    return [
        {"dept_id": r[0], "name": r[1], "status": r[2], "created_at": str(r[3])} for r in rows
    ]


async def dept_stats() -> list[dict]:
    """各团队库状态：表数/总行数（information_schema 枚举）。

    2026-09-17：最近同步字段（取自 sync_log）随数据查询线下线删除；
    库本身保留（不再有新数据写入），表数/行数是存量快照。
    """
    depts = await list_depts()
    stats = []
    for d in depts:
        if d["dept_id"] == "dept_root":
            continue  # 平台团队无业务库统计（CEO 行保留，白名单配置用；其库为 tardis_ceo_db 非 dept_ceo_db，统计显示 -）
        db_name = f"{_settings.db_name_prefix}dept_{d['dept_id']}_db"
        table_count = row_count = None
        try:
            conn = await asyncpg.connect(database=db_name, **_admin_db_conn_url())
            try:
                tables = await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name NOT LIKE '\\_%' AND table_name <> '_schema_meta'"
                )
                table_count = len(tables)
                row_count = 0
                # L17（保留现状）：表级串行 COUNT（asyncpg 单连接不支持并行查询；
                # 团队统计性能优化需多连接/缓存方案，暂不引入）
                for t in tables:
                    cnt = await conn.fetchval(f'SELECT COUNT(*) FROM "{t["table_name"]}"')
                    row_count += cnt or 0
            finally:
                await conn.close()
        except (asyncpg.InvalidCatalogNameError, OSError):
            pass  # 库不存在/不可连 → 表数为空，前端显示未建库
        stats.append(
            {
                "dept_id": d["dept_id"],
                "name": d["name"],
                "status": d["status"],
                "table_count": table_count,
                "row_count": row_count,
            }
        )
    return stats


async def drop_dept(dept_id: str) -> None:
    """删除团队（2026-08-18，B4）：全局库级联清理用户数据 + 删团队库 + 删团队行。

    平台团队（ceo/dept_root）受保护；users 删除靠 FK CASCADE 清理
    sessions/chat_messages/feedback/记忆/批处理等全部关联数据。
    """
    if dept_id in ("ceo", "dept_root"):
        raise ValueError(f"平台团队 {dept_id} 不可删除")

    engine = get_global_engine()
    async with engine.begin() as conn:
        row = (await conn.execute(text("SELECT dept_id FROM departments WHERE dept_id=:d"), {"d": dept_id})).first()
        if row is None:
            raise ValueError(f"团队 {dept_id} 不存在")
        # 知识库（kb_documents/kb_chunks/kb_categories 按团队字段直接删）
        await conn.execute(text("DELETE FROM kb_chunks WHERE document_id IN "
                                "(SELECT id FROM kb_documents WHERE department_id=:d)"), {"d": dept_id})
        await conn.execute(text("DELETE FROM kb_categories WHERE dept_id=:d"), {"d": dept_id})
        await conn.execute(text("DELETE FROM kb_documents WHERE department_id=:d"), {"d": dept_id})
        # 用户（FK CASCADE 级联清理会话/消息/反馈/记忆/批处理）
        await conn.execute(text("DELETE FROM users WHERE department_id=:d"), {"d": dept_id})
        # 团队级配置（2026-09-01：dept_tools 白名单 key 已废弃，改清 dept_block 黑名单 key；旧 key 一并清理）
        await conn.execute(text("DELETE FROM system_config WHERE key IN (:t1, :t2, :t3)"),
                           {"t1": f"dept_block.{dept_id}", "t2": f"dept_kb_cats.{dept_id}",
                            "t3": f"dept_tools.{dept_id}"})
        await conn.execute(text("DELETE FROM departments WHERE dept_id=:d"), {"d": dept_id})

    # 删团队业务库（独立 autocommit 连接）
    conn_kw = _admin_db_conn_url()
    try:
        conn = await asyncpg.connect(database="postgres", **conn_kw)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{_settings.db_name_prefix}tardis_dept_{dept_id}_db"')
        finally:
            await conn.close()
    except (OSError, asyncpg.PostgresError) as e:
        # 库删除失败（被占用等）不阻断主流程——记录由日志可见，团队行已删
        import logging
        logging.getLogger(__name__).warning("drop dept %s db failed: %s", dept_id, str(e)[:200])

    # 团队名缓存失效
    _NAME_CACHE["ts"] = 0.0
