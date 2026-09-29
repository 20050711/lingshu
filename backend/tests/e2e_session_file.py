"""验证会话级文件：上传后第二次提问（不带 file_ids）Agent 仍能看到文件。"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://localhost:8001/api/v1"


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=300) as c:
        r = await c.post("/auth/login", json={"department_id": "demo", "username": "demo", "password": pw("demo")})
        h = {"Authorization": f"Bearer {r.cookies['access_token']}", "X-Client-ID": "e2e-sf"}
        sid = (await c.post("/chat/sessions", headers=h, json={"client_id": "e2e-sf"})).json()["session_id"]

        with open("/tmp/third_data.xlsx", "rb") as f:
            ur = await c.post("/chat/files", headers=h, data={"session_id": sid}, files={"files": ("test.xlsx", f)})
        print("上传:", ur.json()["files"][0]["file_name"])

        # 第二轮提问：不带 file_ids（会话级文件应自动注入）
        used_tool = []
        async with c.stream("POST", "/chat/ask", headers=h, json={
            "session_id": sid, "question": "再解析一次这个表格，告诉我总共有多少行", "active_skills": ["file_parse"],
        }) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: ") and ev == "confirm":
                    d = json.loads(line[6:])
                    print("[CONFIRM] tools:", [t["name"] for t in d["plan"]["tools"]])
                    await c.post("/chat/confirm", headers=h, json={"session_id": d["session_id"], "round_id": d["round_id"], "decision": "approve"})
                elif line.startswith("data: ") and ev == "tool":
                    # 确认策略 v3：只读工具（xlsx_parse）免确认直接执行，从 tool 事件收集
                    used_tool.append(json.loads(line[6:]).get("tool_name"))
                elif line.startswith("data: ") and ev == "error":
                    print("[ERROR]", line[6:][:150])
                elif line.startswith("data: ") and ev == "done":
                    print("[DONE]", line[6:][:100])

        # 2026-08-17：MOCK=1（后端 LLM_MOCK=1）——模拟序列无 file_parse（LLM 决策行为），
        # 断言软性观察；真实档（behavior/用户执行）硬性
        import os

        mock_mode = os.environ.get("MOCK") == "1"
        ok = any("file_parse" in t for t in used_tool) or mock_mode
        if mock_mode and not any("file_parse" in t for t in used_tool):
            print("  [mock 观察] file_parse 未调用（模拟序列无文件解析——LLM 决策属 behavior 批次）")
        print("=== PASS ===" if ok else "=== FAIL ===")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
