"""运维解封 IP 冒烟（2026-09-10 同类排查回归）。

**根因**：登录封禁键是**双因子** `auth:block:{ip}:{username}`（`core/security._ip_key`，
2026-08-10 引入——反代下全站同源 IP，纯 IP 键会让任一攻击者的失败连坐全公司 24h），
而运维解封 `DELETE /admin/ip-blacklist/{ip}` 只删 `auth:block:{ip}` → **键对不上，解封完全无效**，
用户被 24h TTL 一直挡着；顺带 `auth:fail:{ip}:{username}` 计数没清，解封后输错一次立刻又被封。

覆盖（自建自删，只碰测试 IP `203.0.113.x` 的 Redis 键，不碰真实账号）：
1. 双因子封禁键存在时解封 → 键被清
2. 失败计数一并清零（否则下一次输错立即重新封禁）
3. 裸键（无 username 形态）兼容清理
4. 响应返回清理条数

用法（后端运行中）: cd backend && source scripts/env_aip.sh && python -u tests/admin_unblock_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

import httpx  # noqa: E402

from app.core.redis import redis_delete, redis_get, redis_set  # noqa: E402

BASE = "http://localhost:8001/api/v1"
IPS = ("203.0.113.51", "203.0.113.52", "203.0.113.53")
_USER = "smoke_unblock_target"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


async def main() -> int:
    c = httpx.AsyncClient(base_url=BASE, timeout=30)
    try:
        r = await c.post("/auth/login", json={"department_id": "dept_root", "username": "admin",
                                              "password": pw("admin")})
        if r.status_code != 200:
            print("  admin 登录失败:", r.status_code, r.text[:120])
            return 1
        h = {"Cookie": f"access_token={r.cookies.get('access_token')}"}

        # 1. 双因子封禁键（security._ip_key 的真实形态）+ 失败计数
        ip = IPS[0]
        await redis_set(f"auth:block:{ip}:{_USER}", "1", 86400)
        await redis_set(f"auth:fail:{ip}:{_USER}", "9", 3600)
        r = await c.delete(f"/admin/ip-blacklist/{ip}", headers=h)
        body = r.json() if r.status_code == 200 else {}
        check("解封接口 200", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
        check("双因子封禁键已清（修前残留→解封无效）",
              await redis_get(f"auth:block:{ip}:{_USER}") is None)
        check("失败计数一并清零（防解封后立刻再封）",
              await redis_get(f"auth:fail:{ip}:{_USER}") is None)
        check("响应带清理条数", body.get("unblocked") == 1 and body.get("failures_cleared") == 1, str(body))

        # 2. 裸键形态兼容（历史/手工写入）
        ip2 = IPS[1]
        await redis_set(f"auth:block:{ip2}", "1", 86400)
        r = await c.delete(f"/admin/ip-blacklist/{ip2}", headers=h)
        check("裸键形态也清（兼容旧键）",
              r.status_code == 200 and await redis_get(f"auth:block:{ip2}") is None, str(r.json()))

        # 3. 无键时幂等（不报错）
        ip3 = IPS[2]
        r = await c.delete(f"/admin/ip-blacklist/{ip3}", headers=h)
        check("无键解封幂等", r.status_code == 200 and r.json().get("unblocked") == 0, str(r.json()))
    finally:
        for ip in IPS:
            await redis_delete(f"auth:block:{ip}", f"auth:fail:{ip}",
                               f"auth:block:{ip}:{_USER}", f"auth:fail:{ip}:{_USER}")
        await c.aclose()
    print(f"=== {'PASS' if FAIL == 0 else 'FAIL'}（{PASS} 过 / {FAIL} 败）===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
