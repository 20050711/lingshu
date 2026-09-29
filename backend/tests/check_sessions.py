"""检查：会话列表与历史消息是否正常（排查"历史会话空白"）。"""
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
        print(f"会话数: {len(sessions)}")
        for s in sessions[:6]:
            mid = (await c.get(f"/chat/sessions/{s['id']}/messages", headers=h)).json()
            msgs = mid["messages"]
            print(f"  {s['id'][:8]} | title={s['title']!r} | messages={len(msgs)} | readonly={s['is_readonly']}")
            for m in msgs[:2]:
                print(f"      {m['role']}: {(m.get('content') or '')[:40]!r}")


if __name__ == "__main__":
    asyncio.run(main())
