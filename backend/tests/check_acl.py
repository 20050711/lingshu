"""验证 outputs 路由权限：无 token 401 / 本人会话 200 / 他人会话 403。"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://localhost:8001/api/v1"


async def main() -> None:
    # 用完整 URL（避免 httpx base_url 与绝对路径 url 合并歧义）
    async with httpx.AsyncClient(timeout=60) as c:
        # 登录两个用户
        mk = (await c.post(f"{BASE}/auth/login", json={"department_id": "demo", "username": "demo", "password": pw("demo")})).cookies["access_token"]
        ceo = (await c.post(f"{BASE}/auth/login", json={"department_id": "ceo", "username": "ceo", "password": pw("ceo")})).cookies["access_token"]

        # demo 创建会话并生成文件
        sid = (await c.post(f"{BASE}/chat/sessions", headers={"Authorization": f"Bearer {mk}"}, json={"client_id": "acl"})).json()["session_id"]

        from app.agent.tools import ToolContext, get_tool

        ctx = ToolContext(sid, 1, "demo", "employee", "acl", f"/data/outputs/{sid}/1")
        r = await get_tool("doc_export").handler({"format": "docx", "title": "acl", "sections": [{"heading": "h", "content": "x"}]}, ctx)
        file_url = f"http://localhost:8001{r['file_path']}"

        # 1. 无 token（清 cookie jar——L11 token 在 httpOnly cookie，httpx 会残留前面登录的 cookie）
        c.cookies.clear()
        r1 = await c.get(file_url)
        # 2. demo 本人
        r2 = await c.get(file_url, headers={"Authorization": f"Bearer {mk}"})
        # 3. ceo（他人会话）
        r3 = await c.get(file_url, headers={"Authorization": f"Bearer {ceo}"})

        print(f"无 token: {r1.status_code}（期望 401）")
        print(f"本人: {r2.status_code}（期望 200）")
        print(f"他人: {r3.status_code}（期望 403）")
        ok = r1.status_code == 401 and r2.status_code == 200 and r3.status_code == 403
        print("=== PASS ===" if ok else "=== FAIL ===")


if __name__ == "__main__":
    asyncio.run(main())
