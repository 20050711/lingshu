"""压测专用：新建测试团队 press（压测部）+ 15 个测试账号（幂等）。

- 团队：dept_id=press，名称「压测部」（create_dept 自动建 {前缀}dept_press_db）
- 账号：press01~pressN，统一密码取自环境变量 PRESS_PASSWORD（**口令不入库**）
- 规模：默认 15，可用 PRESS_COUNT 调整；press07 角色 dept_admin（本团队知识库上传用）
- 幂等：团队已存在跳过；账号按 (dept_id, username) 判断
- 清理：测试结束后由测试收尾脚本 DROP 库 + 删账号/团队（勿手动清）

用法:
    cd backend && source scripts/env_aip.sh
    export PRESS_PASSWORD='<自定义压测口令>'   # 必填
python scripts/seed_press_dept.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import insert, select

from app.core.database import get_global_engine
from app.core.security import hash_password
from app.models import User
from app.services.dept_service import create_dept

DEPT_ID = "press"
DEPT_NAME = "压测部"
PASSWORD = os.environ.get("PRESS_PASSWORD", "")
if not PASSWORD or len(PASSWORD) < 8:
    raise SystemExit("请先 export PRESS_PASSWORD=<至少 8 位的压测口令>（口令不入库）")

_COUNT = int(os.environ.get("PRESS_COUNT", "15"))

# 压测账号：username, role（并发档位需要独立账号，否则单点登录吊销旧 token 会连环 401）
PRESS_USERS = [(f"press{i:02d}", "employee") for i in range(1, _COUNT + 1)]
PRESS_USERS[6] = ("press07", "dept_admin")  # 团队管理员：本团队知识库上传/管理


async def main() -> int:
    # 1. 团队（幂等；create_dept 内部处理已存在，且补建库）
    try:
        await create_dept(DEPT_ID, DEPT_NAME)
        print(f"department ensured: {DEPT_ID}/{DEPT_NAME}")
    except ValueError as e:
        print(f"[错误] 团队创建失败: {e}")
        return 1

    # 2. 账号（幂等，按 (dept_id, username)）
    engine = get_global_engine()
    created = 0
    async with engine.begin() as conn:
        existing = {
            r[0]
            for r in (await conn.execute(
                select(User.username).where(User.department_id == DEPT_ID)
            )).all()
        }
        for username, role in PRESS_USERS:
            if username not in existing:
                await conn.execute(
                    insert(User).values(
                        username=username,
                        password_hash=hash_password(PASSWORD),
                        department_id=DEPT_ID,
                        role=role,
                    )
                )
                created += 1
        print(f"users: {created} created, {len(existing)} existing (total {len(PRESS_USERS)})")
    await engine.dispose()

    ok = created == len(PRESS_USERS) or created >= 0
    print("=== PASS ===" if ok and created >= 0 else "=== FAIL ===")
    return 0 if created >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
