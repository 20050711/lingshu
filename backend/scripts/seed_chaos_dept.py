"""乱操作 E2E 专用：新建测试团队 chaos（乱操部）+ 测试账号（幂等）。

- 团队：dept_id=chaos，名称「乱操部」（create_dept 自动建 dept_chaos_db）
- 账号：chaos01，口令从 E2E_CHAOS_PASSWORD 环境变量读（不写进仓库）
- 刻意不配 system_config 的 model_layer.chaos → 模型配置回落 model_layer.default
  （实测已是 deepseek-v4-flash + thinking:false 最便宜档，测试零配置成本）
- 幂等：团队已存在跳过；账号按 (dept_id, username) 判断
- 清理：测试结束后 DROP dept_chaos_db + 删账号/团队（勿手动清）

用法:
    cd backend && source scripts/env_aip.sh && python scripts/seed_chaos_dept.py
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

DEPT_ID = "chaos"
DEPT_NAME = "乱操部"
USERNAME = "chaos01"
PASSWORD = os.environ.get("E2E_CHAOS_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("请先设置环境变量 E2E_CHAOS_PASSWORD（该口令不入库）")
ROLE = "employee"


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
        if USERNAME not in existing:
            await conn.execute(
                insert(User).values(
                    username=USERNAME,
                    password_hash=hash_password(PASSWORD),
                    department_id=DEPT_ID,
                    role=ROLE,
                )
            )
            created = 1
        print(f"user {USERNAME}: {'created' if created else 'already exists'}")
    await engine.dispose()

    print("=== PASS ===" if created >= 0 else "=== FAIL ===")
    return 0 if created >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
