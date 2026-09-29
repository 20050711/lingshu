"""R11（2026-08-19）：测试弱密码账号加固——随机强密码化并输出密码表。

目标账号：测试团队各账号（原始口令见部署记录，不在仓库中）。
用法（幂等——已有强密码的账号跳过）：
  cd backend && source scripts/env_aip.sh && python scripts/harden_test_accounts.py
"""
from __future__ import annotations

import asyncio
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend 目录（脚本在 scripts/ 下）

from app.core.database import get_global_engine
from app.core.security import hash_password
from sqlalchemy import text

# 弱密码账号清单：(department_id, username) → 原弱密码（仅用于识别，不用于重置判断）
# 2026-08-19 实测：test 1-15 账号在**团队 test**（用户名=数字 1-15），非 test1-15 团队
WEAK_ACCOUNTS: list[tuple[str, str]] = [
    *[("test", f"{i}") for i in range(1, 16)],
    ("test", "tester"),
    ("abuse", "abuse_y"),
]

_PW_ALPHABET = string.ascii_letters + string.digits + "!@#$%"


def _rand_pw() -> str:
    """16 位随机强密码（保证含字母/数字/符号）。"""
    chars = [
        secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits), secrets.choice("!@#$%"),
    ]
    return "".join(chars) + "".join(secrets.choice(_PW_ALPHABET) for _ in range(12))


async def main() -> None:
    engine = get_global_engine()
    updated: list[tuple[str, str, str]] = []
    async with engine.connect() as conn:
        rows = (await conn.execute(
            text("SELECT id, username, department_id FROM users "
                 "WHERE department_id IN (SELECT dept_id FROM departments "
                 "WHERE dept_id LIKE 'test%' OR dept_id='test' OR dept_id='abuse')")
        )).all()
        by_key = {(r.department_id, r.username): r.id for r in rows}
    async with engine.begin() as conn:
        for dept, uname in WEAK_ACCOUNTS:
            uid = by_key.get((dept, uname))
            if uid is None:
                print(f"跳过（不存在）: {dept}/{uname}")
                continue
            pw = _rand_pw()
            await conn.execute(
                text("UPDATE users SET password_hash=:h, token_version=token_version+1 WHERE id=:id"),
                {"h": hash_password(pw), "id": uid},
            )
            updated.append((dept, uname, pw))
    print(f"\n已加固 {len(updated)} 个弱密码账号（旧 token 已全部吊销）:\n")
    for dept, uname, pw in updated:
        print(f"  {dept}/{uname}  →  {pw}")
    print("\n请将上表记入 docs/交接文档/HANDOVER-2026-08-19.md（R11 新密码表）")


asyncio.run(main())
