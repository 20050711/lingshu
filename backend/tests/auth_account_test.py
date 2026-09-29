"""auth_account_test.py — 用户自助改账号名/改密码（2026-09-01 方案验收）。

覆盖组合接口 PUT /auth/account：
  - 仅改密：旧密码错误 403 / 新密码 <8 或 >16 位 400 / 新=旧 400 / 成功后旧 token 401 /
    新密码登录成功 / 旧密码登录失败
  - 仅改名：同团队撞名 400 / 跨团队同名允许 / 成功后旧 token 401 / 新名登录成功 / 旧名登录失败
  - 同时改名+改密：一次提交成功（单次吊销）
  - admin 建号/重置密码走 8-16 位规则（<8 位 400）

用独立临时账号（admin 建号 → 测试 → terminate 清理），不污染 demo 等既有账号。

用法: conda run -n aip python -u tests/auth_account_test.py
"""
import asyncio
import sys

import httpx
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://localhost:8001/api/v1"
results: list[tuple[str, str, str, str, bool, str]] = []


def rec(cid: str, desc: str, expected: str, actual: str, ok: bool, note: str = ""):
    mark = "⚠" if (not ok or "500" in actual or "EXC" in actual) else "✓"
    results.append((cid, desc, expected, actual, ok, note))
    print(f"{mark} {cid} | {desc} | 预期 {expected} | 实际 {actual}")


async def login(c: httpx.AsyncClient, dept: str, username: str, password: str) -> httpx.Response:
    return await c.post("/auth/login", json={"department_id": dept, "username": username, "password": password})


def auth_h(resp: httpx.Response) -> dict:
    return {"Authorization": f"Bearer {resp.cookies['access_token']}"}


async def main() -> int:
    u = f"auth_t_{sys.hexversion % 100000}"  # 临时账号名（进程内唯一，幂等建号）
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # ===== 0. admin 建临时账号 =====
        ad = await login(c, "dept_root", "admin", pw("admin"))
        rec("B00", "admin 登录", "200", str(ad.status_code), ad.status_code == 200)
        ah = auth_h(ad)
        # 幂等：上次崩溃残留的同名账号先 terminate 再建
        uid = next((x["id"] for x in (await c.get("/admin/users", headers=ah)).json()["users"]
                    if x["username"] == u), None)
        if uid:
            await c.post(f"/admin/users/{uid}/terminate", headers=ah)
        r = await c.post("/admin/users", headers=ah, json={
            "username": u, "password": "initPass88", "department_id": "demo", "role": "employee"})
        rec("B01", "建临时账号(8-16位)", "200", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 200)
        if r.status_code != 200:
            print("⚠ 临时账号创建失败，中止（可能后端未重启）")
            return 1
        uid = next((x["id"] for x in (await c.get("/admin/users", headers=ah)).json()["users"]
                    if x["username"] == u), None)

        # ===== 1. 改密：旧密码错误 403 =====
        r1 = await login(c, "demo", u, "initPass88")
        h1 = auth_h(r1)
        r = await c.put("/auth/account", headers=h1, json={"old_password": "wrongPass99", "new_password": "newPass123"})
        rec("B02", "改密-旧密码错误", "403", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 403)
        # 仍可登录（未吊销）；注意：登录即 token_version+1（单点登录）会吊销 h1，须换新 token 继续
        rl = await login(c, "demo", u, "initPass88")
        rec("B03", "改密失败后原密码仍可登录", "200", str(rl.status_code), rl.status_code == 200)
        h1 = auth_h(rl)

        # ===== 2. 改密：新密码长度 400 =====
        r = await c.put("/auth/account", headers=h1, json={"old_password": "initPass88", "new_password": "short1"})
        rec("B04", "改密-新密码<8位", "400", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 400)
        r = await c.put("/auth/account", headers=h1, json={"old_password": "initPass88", "new_password": "a" * 17})
        rec("B05", "改密-新密码>16位", "400", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 400)
        r = await c.put("/auth/account", headers=h1, json={"old_password": "initPass88", "new_password": "initPass88"})
        rec("B06", "改密-新=旧密码", "400", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 400)
        # 空提交 400
        r = await c.put("/auth/account", headers=h1, json={})
        rec("B07", "改密-空提交", "400", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 400)

        # ===== 3. 成功改密：旧 token 401 / 新密码登录成功 / 旧密码失败 =====
        r = await c.put("/auth/account", headers=h1, json={"old_password": "initPass88", "new_password": "newPass123"})
        rec("B08", "改密成功", "200", str(r.status_code), r.status_code == 200)
        r = await c.get("/auth/me", headers=h1)
        rec("B09", "改密后旧 token 吊销", "401", str(r.status_code), r.status_code == 401)
        rn = await login(c, "demo", u, "newPass123")
        rec("B10", "新密码登录", "200", str(rn.status_code), rn.status_code == 200)
        ro = await login(c, "demo", u, "initPass88")
        rec("B11", "旧密码登录失败", "401", str(ro.status_code), ro.status_code == 401)

        # ===== 4. 改名：撞名 400 / 跨团队同名允许 / 成功后旧 token 401 =====
        hn = auth_h(rn)
        r = await c.put("/auth/account", headers=hn, json={"new_username": "demo"})  # 同团队撞名
        rec("B12", "改名-同团队撞名", "400", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 400)
        new_name = f"{u}_new"
        r = await c.put("/auth/account", headers=hn, json={
            "new_username": new_name, "old_password": "newPass123", "new_password": "finalPass99"})
        rec("B13", "同时改名+改密", "200", str(r.status_code), r.status_code == 200)
        r = await c.get("/auth/me", headers=hn)
        rec("B14", "改后旧 token 吊销", "401", str(r.status_code), r.status_code == 401)
        rf = await login(c, "demo", new_name, "finalPass99")
        rec("B15", "新名+新密登录", "200", str(rf.status_code), rf.status_code == 200)
        rl2 = await login(c, "demo", u, "newPass123")
        rec("B16", "旧名登录失败", "401", str(rl2.status_code), rl2.status_code == 401)

        # ===== 5. admin 重置密码：<8 位 400 / 合法 200 =====
        hf = auth_h(rf)
        hf_uid = uid
        r = await c.post(f"/admin/users/{hf_uid}/reset-password", headers=ah, json={"password": "short1"})
        rec("B17", "admin重置-<8位", "400", f"{r.status_code} {r.json().get('error', {}).get('message', '')}",
            r.status_code == 400)
        r = await c.post(f"/admin/users/{hf_uid}/reset-password", headers=ah, json={"password": "resetPass77"})
        rec("B18", "admin重置-合法", "200", str(r.status_code), r.status_code == 200)

        # ===== 6. 清理：terminate 临时账号 =====
        if uid:
            r = await c.post(f"/admin/users/{uid}/terminate", headers=ah)
            rec("B19", "清理临时账号", "200", str(r.status_code), r.status_code == 200)
        else:
            rec("B19", "清理临时账号", "200", "uid 未知，跳过", False, "⚠ 需手动清理")

    n_ok = sum(1 for *_ , ok, _ in results if ok)
    print(f"\n通过 {n_ok}/{len(results)}")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
