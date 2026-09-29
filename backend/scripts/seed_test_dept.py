"""前端 E2E 测试团队：test（测试部）+ 15 个账号（账号与密码均为 1-15，幂等）。

- 密码为 1-2 位数字（用户指定，便于手动登录）——走 SQL 插入绕过创建 API 的 min_length=6
  校验；仅测试团队使用，勿用于生产账号
- 团队：dept_id=test，名称「测试部」（create_dept 自动建 dept_test_db）
- 账号：username="1"~"15"，password="1"~"15"，role=employee
- 清理：测试结束后 DROP dept_test_db + 删账号/团队（勿手动清）

用法:
    cd backend && source scripts/env_aip.sh && python scripts/seed_test_dept.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import insert, select

from app.core.database import get_global_engine
from app.core.security import hash_password
from app.models import User
from app.services.dept_service import create_dept

DEPT_ID = "test"
DEPT_NAME = "测试部"

# 15 个账号：username/password 均为 1-15
TEST_USERS = [(str(i), str(i)) for i in range(1, 16)]


async def main() -> int:
    try:
        await create_dept(DEPT_ID, DEPT_NAME)
        print(f"department ensured: {DEPT_ID}/{DEPT_NAME}")
    except ValueError as e:
        print(f"[错误] 团队创建失败: {e}")
        return 1

    engine = get_global_engine()
    created = 0
    async with engine.begin() as conn:
        existing = {
            r[0]
            for r in (await conn.execute(
                select(User.username).where(User.department_id == DEPT_ID)
            )).all()
        }
        for username, password in TEST_USERS:
            if username not in existing:
                await conn.execute(
                    insert(User).values(
                        username=username,
                        password_hash=hash_password(password),
                        department_id=DEPT_ID,
                        role="employee",
                    )
                )
                created += 1
        print(f"users: {created} created, {len(existing)} existing (total {len(TEST_USERS)})")
    await engine.dispose()

    print("=== PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
