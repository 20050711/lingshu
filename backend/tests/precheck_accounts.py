"""测试前账号预检（P3，2026-08-31）：登录全部测试账号并打印 token_version 基线。

用途：跑 e2e 套件前先执行本脚本——单点登录设计（登录即 token_version+1）下，测试期间
同账号任何并发登录都会把测试 token 顶掉（下一请求 401，结果失真）。本脚本确认账号可登录，
并提示测试期间请勿用这些账号登录平台。

用法：cd backend && source scripts/env_aip.sh && python -u tests/precheck_accounts.py
零费用：不调用任何 LLM。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

from app.core.database import get_global_engine

BASE = "http://localhost:8001/api/v1"

# (团队, 用户名, 密码, 用途) —— 本地回归测试账号（2026-08-10 换密后）
ACCOUNTS = [
    ("demo", "demo", pw("demo"), "e2e_customer/e2e_ask 员工"),
    ("demo", "demo_admin", pw("demo_admin"), "e2e_customer 团队管理员"),
    ("dept_root", "admin", pw("admin"), "admin 运维"),
    ("ceo", "ceo", pw("ceo"), "ceo 全局"),
    ("demo", "walkthrough", pw("walkthrough"), "人工走查专用"),
]


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        rows = []
        for dept, username, password, use in ACCOUNTS:
            try:
                r = await c.post("/auth/login", json={"department_id": dept, "username": username,
                                                       "password": password})
                if r.status_code != 200:
                    print(f"✗ {username} 登录失败: {r.status_code} {r.text[:120]}")
                    continue
                ver = None
                async with get_global_engine().connect() as conn:
                    row = (await conn.execute(
                        text("SELECT token_version FROM users WHERE username=:n AND department_id=:d"),
                        {"n": username, "d": dept})).first()
                    ver = row[0] if row else None
                rows.append((username, "OK", ver, use))
            except Exception as e:
                print(f"✗ {username} 预检异常: {str(e)[:120]}")
    print("\n=== 账号预检结果（token_version 基线） ===")
    for username, status, ver, use in rows:
        print(f"  ✓ {username:14s} {status}  ver={ver}   {use}")
    print("\n⚠ 提示：单点登录（登录即 token_version+1）——测试期间请勿用以上账号登录平台，")
    print("  否则会顶掉测试 token 导致 401 误报。建议用 walkthrough 账号人工走查与测试并行。")
    ok = len(rows) == len(ACCOUNTS)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
