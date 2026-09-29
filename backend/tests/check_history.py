"""验证历史会话接口返回 tool_events/files/charts 完整记录。"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://localhost:8001/api/v1"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        r = await c.post("/auth/login", json={"department_id": "demo", "username": "demo", "password": pw("demo")})
        h = {"Authorization": f"Bearer {r.cookies['access_token']}"}
        sessions = (await c.get("/chat/sessions", headers=h)).json()
        if not sessions:
            print("无会话")
            return
        sid = sessions[0]["id"]
        d = (await c.get(f"/chat/sessions/{sid}/messages", headers=h)).json()
        print("messages:", len(d["messages"]), "| charts:", len(d["charts"]))
        for m in d["messages"][:4]:
            print(" ", m["role"], "| tool_events:", bool(m.get("tool_events")), "| files:", bool(m.get("files")))
        if d["charts"]:
            print("chart option keys:", list(d["charts"][0]["option"].keys())[:5])


if __name__ == "__main__":
    asyncio.run(main())
