"""数据库引擎管理。

- global_engine: 全局库（用户/会话/知识库/管理）
- 团队库按 dept_id 动态创建连接（PRD：团队间数据物理隔离）
- read_only 事务：业务查询全部走只读（Agent 空间隔离基线）
"""
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

_global_engine: AsyncEngine | None = None
_dept_engines: dict[str, AsyncEngine] = {}


def _engine_kwargs() -> dict:
    """2026-08-10：pool_pre_ping——取消风暴（用户终止对话 → CancelledError 级联）会把
    asyncpg 连接置为坏状态（"Exception terminating connection"），池不重建则后续 ask
    拿到坏连接直接 InterfaceError（实测：终止后下一条消息"connection is closed"静默失败）。
    pre_ping 取连接前探活，坏连接自动废弃重建；本地 PG 开销可忽略。"""
    return {"pool_size": 10, "max_overflow": 10, "pool_pre_ping": True}


def get_global_engine() -> AsyncEngine:
    global _global_engine
    if _global_engine is None:
        _global_engine = create_async_engine(_settings.global_db_url, **_engine_kwargs())
    return _global_engine


# 2026-09-17：get_personal_engine 随数据查询线下线删除（个人库已无读写方；存量库原地保留）


def get_dept_engine(dept_id: str) -> AsyncEngine:
    """团队库引擎（缓存）。dept_id 为 'ceo' 时走 tardis_ceo_db。

    SEC-19（2026-08-17）：dept_id 直拼 DB URL 前白名单校验（防 URL 注入改连接目标）。
    """
    from app.core.file_utils import is_valid_dept_id

    if dept_id != "ceo" and not is_valid_dept_id(dept_id):
        raise ValueError(f"非法团队标识: {dept_id!r}")
    key = "ceo" if dept_id == "ceo" else dept_id
    if key not in _dept_engines:
        url = (
            _settings.ceo_db_url
            if key == "ceo"
            else _settings.dept_db_url_template.format(dept_id=dept_id)
        )
        _dept_engines[key] = create_async_engine(
            url, pool_size=15, max_overflow=10, pool_pre_ping=True  # 2026-08-11：default 队列 15 联动
        )
    return _dept_engines[key]


async def dispose_engines() -> None:
    global _global_engine
    if _global_engine is not None:
        await _global_engine.dispose()
        _global_engine = None
    for eng in _dept_engines.values():
        await eng.dispose()
    _dept_engines.clear()
