"""四期重构数据库迁移脚本（幂等，可重复执行）。

1. 账号：users 唯一约束 username → (department_id, username)；departments dept_root 更名"运维管理"
2. 记忆：ceo_memory → user_memory（表 + 列 account_id → user_id）
3. 技能：skill_files 表 + user_skill_prefs 复合技能 id 展开为新工具 id

用法（先停 uvicorn 再执行，脚本开头会检查 8000 端口）：
    cd backend && source scripts/env_aip.sh && python scripts/migrate_refactor.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.database import get_global_engine

# 复合技能 id → 新工具集展开（四期重构）
PREF_EXPAND = {
    "chart": ["sql_query", "generate_chart"],
    "multi": ["sql_query"],
    "report": ["file_parse", "sql_query", "generate_chart", "doc_export"],
    "sheet": ["file_parse", "sql_query"],
    "docparse": ["file_parse", "image_recognition"],
    "kb": ["file_search"],
    "web": ["web_search"],
    "imgreco": ["image_recognition"],
    "imggen": ["image_generation"],
    "ppt": ["doc_export"],
    "memory": ["memory"],
    "code_exec": ["run_script"],
    # writing/approval/contract（纯文本/规划）→ 丢弃
}
# 新工具全集（校验展开结果）
ALL_TOOLS = {
    "sql_query", "file_parse", "generate_chart", "doc_export", "web_search",
    "file_search", "image_recognition", "image_generation", "memory", "run_script",
}


async def _check_port() -> None:
    import socket

    s = socket.socket()
    try:
        s.connect(("127.0.0.1", 8000))
        print("⚠️  检测到后端正在运行（8000 端口）。请先停 uvicorn 再执行迁移！")
        sys.exit(1)
    except OSError:
        pass
    finally:
        s.close()


async def _migrate_accounts(conn) -> None:
    # 0. users.token_version（L21 按用户吊销；幂等）
    await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0"))
    print("✓ users.token_version 列就绪（L21 按用户吊销）")

    # 1a. username 查重（跨团队同名会违反新约束 → abort）
    dups = (
        await conn.execute(
            text("SELECT username, COUNT(*) FROM users GROUP BY username HAVING COUNT(*) > 1")
        )
    ).all()
    if dups:
        print(f"❌ 存在跨团队同名账号 {[r[0] for r in dups]}，请人工处理后重试")
        sys.exit(1)
    # 1b. 删旧唯一约束 + 加联合唯一
    has_old = (
        await conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname='users_username_key' AND conrelid='users'::regclass")
        )
    ).first()
    if has_old:
        await conn.execute(text("ALTER TABLE users DROP CONSTRAINT users_username_key"))
        print("✓ users 旧唯一约束 users_username_key 已删除")
    has_new = (
        await conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname='uq_users_dept_username'")
        )
    ).first()
    if not has_new:
        await conn.execute(
            text("ALTER TABLE users ADD CONSTRAINT uq_users_dept_username UNIQUE (department_id, username)")
        )
        print("✓ users 联合唯一约束 uq_users_dept_username 已添加")
    # 1c. dept_root 更名
    r = await conn.execute(
        text("UPDATE departments SET name='运维管理' WHERE dept_id='dept_root' AND name != '运维管理'")
    )
    if r.rowcount:
        print("✓ departments.dept_root 已更名'运维管理'")


async def _migrate_memory(conn) -> None:
    has_old = (
        await conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='ceo_memory'")
        )
    ).first()
    has_new = (
        await conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='user_memory'")
        )
    ).first()
    if has_old and not has_new:
        await conn.execute(text("ALTER TABLE ceo_memory RENAME TO user_memory"))
        await conn.execute(text("ALTER TABLE user_memory RENAME COLUMN account_id TO user_id"))
        print("✓ ceo_memory → user_memory（列 account_id → user_id）")
    else:
        print("· 记忆表已是 user_memory（跳过）")


async def _migrate_skills(conn) -> None:
    # 3a. skill_files 表
    await conn.execute(
        text("""
            CREATE TABLE IF NOT EXISTS skill_files (
              id SERIAL PRIMARY KEY,
              dept_id VARCHAR(50) NOT NULL,
              skill_name VARCHAR(100) NOT NULL,
              description TEXT,
              content TEXT NOT NULL,
              tools TEXT,
              status VARCHAR(20) NOT NULL DEFAULT 'active',
              sort_order INT NOT NULL DEFAULT 0,
              created_by INT, updated_by INT,
              created_at TIMESTAMP NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
              UNIQUE (dept_id, skill_name)
            )""")
    )
    # 3b. user_skill_prefs 展开迁移
    rows = (
        await conn.execute(
            text("SELECT id, user_id, skill_id, enabled FROM user_skill_prefs WHERE skill_id != '__auto__'")
        )
    ).all()
    expanded = 0
    for r in rows:
        target = PREF_EXPAND.get(r.skill_id)
        if target is None:
            # 未知旧 id 或已展开的新工具 id：原样保留
            continue
        if r.enabled:
            for tid in target:
                exists = (
                    await conn.execute(
                        text("SELECT 1 FROM user_skill_prefs WHERE user_id=:u AND skill_id=:s"),
                        {"u": r.user_id, "s": tid},
                    )
                ).first()
                if not exists:
                    await conn.execute(
                        text("INSERT INTO user_skill_prefs (user_id, skill_id, enabled) VALUES (:u, :s, TRUE)"),
                        {"u": r.user_id, "s": tid},
                    )
                    expanded += 1
        # 旧复合 id 行删除（含 disabled 的）
        await conn.execute(text("DELETE FROM user_skill_prefs WHERE id=:id"), {"id": r.id})
    if expanded:
        print(f"✓ user_skill_prefs 展开迁移：新增 {expanded} 条工具勾选（旧复合技能 id 已替换）")
    else:
        print("· user_skill_prefs 无待展开行（或已迁移）")


async def main() -> None:
    await _check_port()
    engine = get_global_engine()
    async with engine.begin() as conn:
        print("== ① 账号 ==")
        await _migrate_accounts(conn)
        print("== ② 记忆 ==")
        await _migrate_memory(conn)
        print("== ③ 技能 ==")
        await _migrate_skills(conn)
    # 校验输出
    async with engine.connect() as conn:
        cons = (await conn.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='users'::regclass"))).all()
        print("\n== 校验 ==")
        print("users 约束:", [c[0] for c in cons])
        tables = (await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
            "AND table_name IN ('user_memory','skill_files') ORDER BY table_name"))).all()
        print("新表存在:", [t[0] for t in tables])
    print("迁移完成 ✅（可重复执行）")


if __name__ == "__main__":
    asyncio.run(main())
