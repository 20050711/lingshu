"""验证图片产出：done 事件含 image 类型 + 鉴权下载。"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://localhost:8001/api/v1"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=200) as c:
        r = await c.post("/auth/login", json={"department_id": "demo", "username": "demo", "password": pw("demo")})
        h = {"Authorization": f"Bearer {r.cookies['access_token']}", "X-Client-ID": "img-check"}
        sid = (await c.post("/chat/sessions", headers=h, json={"client_id": "img-check"})).json()["session_id"]
        done_outputs = []
        async with c.stream(
            "POST", "/chat/ask", headers=h,
            json={"session_id": sid, "question": "生成一张简约的蓝色海报", "active_skills": ["image_generation"]},
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: ") and ev == "confirm":
                    d = json.loads(line[6:])
                    await c.post(
                        "/chat/confirm", headers=h,
                        json={"session_id": d["session_id"], "round_id": d["round_id"], "decision": "approve"},
                    )
                elif line.startswith("data: ") and ev == "done":
                    d = json.loads(line[6:])
                    done_outputs = d["outputs"]
        print("done.outputs:", json.dumps(done_outputs, ensure_ascii=False)[:300])
        img = next((o for o in done_outputs if o.get("type") == "image"), None)
        print("image 产出:", img is not None)
        if img:
            rr = await c.get("http://localhost:8001" + img["file_path"], headers=h)
            print("鉴权下载:", rr.status_code)


if __name__ == "__main__":
    asyncio.run(main())
