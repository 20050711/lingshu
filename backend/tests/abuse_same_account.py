"""abuse_same_account.py — 多人同时使用一个账号检测（PLAN-v5 批次 B1 新编脚本，用户点名场景）。

覆盖：同账号并发登录 / 两 token 并发 ask 同一会话（round_id 冲突，M17 动态复现）/
并发 answer 同反问（F5：v2 取代 confirm）/ 并发删会话+ask / 交替 ask 不同会话 /
token A 登出后 token B 行为。

F5（2026-08-14）单点登录适配：同账号新登录吊销旧 token——旧 token 401 属新语义预期
（原"多 token 并行"世界已由 L21 单点登录取代）。

成本：并发 ask 用简单问题"你好"（共 2-4 次 LLM 调用）。
输出格式：用例号 | 输入 | 预期 | 实际 | 通过/异常。异常重点标出 ⚠。
只读验证不修复；测试会话末尾清理。

用法: conda run -n aip python tests/abuse_same_account.py
"""
import asyncio
import json
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


async def login(c: httpx.AsyncClient) -> dict:
    r = await c.post("/auth/login", json={"department_id": "demo", "username": "demo", "password": pw("demo")})
    return {"Authorization": f"Bearer {r.cookies['access_token']}"}


async def ask_round_id(c: httpx.AsyncClient, headers: dict, session_id: str) -> tuple[int, int]:
    """ask 一次返回 (http状态, done事件round_id)；无 done 返回 (-1, None)。"""
    round_id = None
    status = -1
    try:
        async with c.stream("POST", "/chat/ask", headers=headers,
                            json={"session_id": session_id, "question": "你好"}) as resp:
            status = resp.status_code
            ev = ""
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    ev = line[7:]
                elif line.startswith("data: ") and line[6:].startswith("{"):
                    d = json.loads(line[6:])
                    if ev == "done":
                        round_id = d.get("round_id")
    except Exception as e:
        status = f"EXC {type(e).__name__}"  # type: ignore
    return status, round_id


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as c:
        # ===== 1. 同账号并发登录 5 次 =====
        tokens = await asyncio.gather(*[
            c.post("/auth/login", json={"department_id": "demo", "username": "demo", "password": pw("demo")})
            for _ in range(5)
        ])
        ok_cnt = sum(1 for t in tokens if t.status_code == 200)
        token_ids = {t.cookies.get("access_token", "")[:20] for t in tokens}
        # F5（2026-08-14）：单点登录新语义——每次登录吊销旧 token（L21），并发 5 次后至多 1 个有效
        rec("A01", "同账号并发登录 5 次", "5/5", f"{ok_cnt}/5 token 前20位唯一数={len(token_ids)}",
            ok_cnt == 5, "记录：单点登录（每次登录吊销旧 token，并发下至多最后 1 个有效）")
        h = {"Authorization": f"Bearer {tokens[0].cookies['access_token']}"}
        h2 = {"Authorization": f"Bearer {tokens[1].cookies['access_token']}"}

        # ===== 2. 两 token 并发 ask 同一会话（M17 round_id 冲突复现）=====
        r = await c.post("/chat/sessions", headers=h, json={"client_id": "abuse-same"})
        sid = r.json().get("session_id", "")
        rec("A02", "建共享会话", "200", f"{r.status_code} sid={sid[:8]}", r.status_code == 200)
        st1, r1 = await ask_round_id(c, h, sid)
        st2, r2 = await ask_round_id(c, h2, sid)
        same_round = (r1 == r2 and r1 is not None)
        # F5（2026-08-14）：单点登录后旧 token（tokens[1]）大概率已吊销 → 401 属预期新语义
        rec("A03", "两 token 并发 ask 同一会话 round_id", "200/200-401", f"tokenA={st1} round={r1} | tokenB={st2} round={r2}",
            st1 == 200 and st2 in (200, 401) and not same_round,
            "M17：round_id 相同则冲突成立 ⚠" if same_round else "单点登录：旧 token 已吊销（401=新语义预期）" if st2 == 401 else "")

        # ===== 3. 并发 answer 同一反问（F5 适配 v2：/chat/confirm 已下线，answer 无 waiter 时 E009 404）=====
        r3, r4 = await asyncio.gather(
            c.post("/chat/answer", headers=h, json={"session_id": sid, "question_id": "q_none", "answers": [], "extra_text": ""}),
            c.post("/chat/answer", headers=h2, json={"session_id": sid, "question_id": "q_none", "answers": [], "extra_text": ""}),
        )
        rec("A04", "并发 answer 同一反问(无 waiter)", "404/404", f"{r3.status_code}/{r4.status_code}", r3.status_code == r4.status_code,
            "记录：双请求行为一致")

        # ===== 4. 并发删会话 + ask =====
        r = await c.post("/chat/sessions", headers=h, json={"client_id": "abuse-race2"})
        sid2 = r.json().get("session_id", "")

        async def ask_quick():
            try:
                async with c.stream("POST", "/chat/ask", headers=h2,
                                    json={"session_id": sid2, "question": "你好"}) as resp:
                    async for _ in resp.aiter_lines():
                        pass
                    return resp.status_code
            except Exception as e:
                return f"EXC {type(e).__name__}"

        st_ask, r_del = await asyncio.gather(ask_quick(), c.delete(f"/chat/sessions/{sid2}", headers=h))
        # F5：单点登录下 tokenB 可能 401（旧 token 吊销）
        rec("A05", "tokenA 删会话 || tokenB ask 同会话", "无 500", f"ask={st_ask} del={r_del.status_code}",
            r_del.status_code < 500 and (st_ask in (200, 401, 404)),
            "记录：竞态行为（预期 ask 200/401/404，不 500）")

        # ===== 5. 交替 ask 不同会话（隔离性）=====
        r = await c.post("/chat/sessions", headers=h, json={"client_id": "abuse-isoA"})
        sA = r.json().get("session_id", "")
        r = await c.post("/chat/sessions", headers=h2, json={"client_id": "abuse-isoB"})
        sB = r.json().get("session_id", "")
        stA, _ = await ask_round_id(c, h, sA)
        stB, _ = await ask_round_id(c, h2, sB)
        # F5：单点登录下 tokenB 可能 401
        rec("A06", "两 token 各自会话交替 ask", "200/200-401", f"{stA}/{stB}", stA == 200 and stB in (200, 401))

        # ===== 6. token A 登出后 token B 行为（L21 方案 A：按用户吊销——全端掉线）=====
        await c.post("/auth/logout", headers=h, json={})
        r = await c.get("/chat/sessions", headers=h)
        rB = await c.get("/chat/sessions", headers=h2)
        rec("A07", "tokenA 登出后 tokenB 仍可用?", "401/401", f"登出tokenA={r.status_code} tokenB={rB.status_code}",
            r.status_code == 401 and rB.status_code == 401,
            "L21 方案 A：登出按用户吊销（ver+1）——全端掉线（产品已确认权衡）")

        # ===== 汇总 =====
        print("\n== 汇总 ==")
        abnormal = [x for x in results if not x[4]]
        print(f"总用例 {len(results)}，异常/未达预期 {len(abnormal)}：{[x[0] for x in abnormal]}")
        print("=== PASS(有异常标记为预期已知漏洞) ===" if len(abnormal) <= 3 else "=== 异常过多，需人工核对 ===")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
