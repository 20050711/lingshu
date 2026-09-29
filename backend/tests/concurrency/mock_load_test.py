"""模拟 LLM 高并发压测（2026-08-17 用户决策：免费模型并发不足，用模拟问答结果+时间做高并发套件）。

原理：后端以 LLM_MOCK=1 启动（config.llm_mock=True）——主 LLM 返回**确定性模拟响应**
（首轮固定 tool_calls 调 file_search → 收到工具结果后返回固定最终回答）+ 模拟延迟 1s/轮
（config.llm_mock_delay_s）。压测平台完整管线（队列/图路由/DB/SSE/落库）而
**零 LLM 费用、零 API 配额占用**，并发数不受免费档 RPM 20-30 限制。

用法:
  1) 启动后端（mock 模式）:
     cd backend && source scripts/env_aip.sh
     LLM_MOCK=1 TRUSTED_PROXIES=127.0.0.1 uvicorn app.main:app --port 8000 --no-proxy-headers &
  2) 跑压测:
     python tests/concurrency/mock_load_test.py [并发数=50]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

BASE = "http://127.0.0.1:8001/api/v1"

# 账号池（登录一次复用 token；50 并发下每账号 1-2 个并发 ask，避开 RATE_LIMIT_ASK_PER_MIN=30/用户）
# test 团队：username/password 均为 "1"~"15"（seed_test_dept.py）
ACCOUNTS = [
    ("demo", "demo", pw("demo")),
    *[(f"press{i:02d}", "press", "Press#2026$Test") for i in range(1, 16)],
    *[(str(i), "test", str(i)) for i in range(1, 16)],
]

PASS, FAIL = 0, 0


async def ask_once(c: httpx.AsyncClient, h: dict, sid: str) -> tuple[bool, str]:
    """单个 ask（mock LLM 下应快速 done），返回 (成功, 摘要)。"""
    done_seen = error_seen = False
    full = ""
    async with c.stream("POST", "/chat/ask", headers=h, json={
        "session_id": sid,
        "question": "统计演示团队各渠道消费并汇总",
        "active_skills": ["file_parse"],
    }) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                try:
                    d = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if ev == "done":
                    done_seen = True
                elif ev == "error":
                    error_seen = True
                    full += str(d)[:80]
                elif ev == "text":
                    full += d.get("delta", "")
    return done_seen and not error_seen, full[:60] or "no-text"


async def worker(c: httpx.AsyncClient, h: dict, idx: int) -> tuple[bool, str]:
    """一个并发 = 建会话 + ask（模拟问答）+ 校验 done。"""
    try:
        r = await c.post("/chat/sessions", headers=h, json={"client_id": f"mock-{idx}"})
        if r.status_code != 200:
            return False, f"建会话 {r.status_code}"
        sid = r.json()["id"]
        ok, brief = await ask_once(c, h, sid)
        return ok, brief
    except Exception as e:
        return False, str(e)[:80]


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    print(f"=== 模拟 LLM 高并发压测（{n} 并发，零真实 LLM）===")
    clients = [httpx.AsyncClient(base_url=BASE, timeout=120) for _ in range(8)]
    headers: list[dict] = []

    # 登录账号池（均匀分配到 8 客户端）
    for i, (u, dept, pw) in enumerate(ACCOUNTS):
        c = clients[i % len(clients)]
        r = await c.post("/auth/login", json={"department_id": dept, "username": u, "password": pw})
        if r.status_code != 200:
            print(f"  ✗ 登录失败 {dept}/{u}: {r.status_code}")
            continue
        headers.append({"Authorization": f"Bearer {r.cookies['access_token']}", "X-Client-ID": f"mock-{u}"})
    if not headers:
        print("无可用账号，终止")
        return 1
    print(f"  账号池 {len(headers)} 个（并发 {n}，每账号 {n // len(headers)} 个并发 ask）")

    # 并发 ask（账号循环分配；同账号并发数 = n/账号数，< 限流阈值）
    t0 = time.time()
    tasks = []
    for i in range(n):
        h = headers[i % len(headers)]
        c = clients[i % len(clients)]
        tasks.append(worker(c, h, i))
    results = await asyncio.gather(*tasks)
    wall = time.time() - t0

    ok_n = sum(1 for ok, _ in results if ok)
    fails = [(i, brief) for i, (ok, brief) in enumerate(results) if not ok]
    print(f"  完成 {ok_n}/{n}  墙钟 {wall:.1f}s  （均值 {wall / n:.2f}s/请求，队列串行部分含在内）")
    if fails:
        print(f"  失败 {len(fails)} 个（前 5）:")
        for i, brief in fails[:5]:
            print(f"    [{i}] {brief}")

    # 健康检查（BASE 已含 /api/v1 前缀）
    hc = clients[0]
    r = await hc.get("/health")
    print(f"  健康检查: {r.status_code} {r.text[:60]}")

    for c in clients:
        await c.aclose()

    ok = ok_n == n
    print("=== PASS ===" if ok else "=== FAIL ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
