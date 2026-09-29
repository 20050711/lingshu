"""2026-08-10 设计检查全量修复 · 验收冒烟（D8/D10/D13/D23/E-02/E-03/E-04/E-10 + B8/B10）。

覆盖（HTTP 或服务层直测，全部自建自删，遵守踩坑 38）：
- D8：persist_round 草稿+终稿只落一条 assistant（服务层单测，mock final_state）
- D10：persist 落库 user 消息与 request_user_content 一致（同上）
- E-10：图表去重只删跨轮（round_id < 当前轮），同轮保留（同上）
- B8：_persist_plan_state 计划轮写 pending / 确认轮复位 none（同上）
- B10：_next_round 原子递增（真实 DB）
- E-02：/outputs 路径穿越拒绝（raw socket，%2F 与明文 ..）
- E-03：非 UUID 参数 422
- E-04：admin 知识文档详情不再 500（列名修复）
- D13：feedback 状态机非法流转 400 / 合法流转通过（HTTP admin）
- D23：kb 下架后检索不可见（HTTP）

用法：cd backend && source scripts/env_aip.sh && python -u tests/design_fix_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.creds import pw  # 密码走环境变量/backend/.env.test（2026-09-10）

import httpx

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


async def _login(c: httpx.AsyncClient, dept: str, user: str, pwd: str) -> str | None:
    r = await c.post("/auth/login", json={"department_id": dept, "username": user, "password": pwd})
    if r.status_code != 200:
        return None
    return c.cookies.get("access_token")


async def test_services() -> None:
    """服务层单测（不依赖真实 LLM）：D6/D8/D10/E-10/B8/B10。"""
    print("[1] 服务层：D6/D8/D10/E-10/B8/B10")
    from app.api.chat import _next_round
    from app.core.database import get_global_engine
    from app.services.chat_service import persist_round

    engine = get_global_engine()
    # 自建会话（自删）
    sid = str(uuid.uuid4())
    async with engine.begin() as conn:
        from sqlalchemy import text as _t

        await conn.execute(
            _t("INSERT INTO sessions (id, user_id, client_id, title, department_id, is_readonly) "
               "VALUES (:id, 1, 'smoke', '冒烟', 'demo', false)"),
            {"id": sid},
        )
    try:
        # B10：原子轮号递增（两次分配不同且递增）
        r1 = await _next_round(engine, sid)
        r2 = await _next_round(engine, sid)
        check("B10 原子轮号递增", r2 == r1 + 1 and r1 >= 1, f"r1={r1} r2={r2}")

        # M1（2026-08-10）：无交付进展检测纯函数 + 阶梯路由（21 轮事故修复核心）
        from app.agent.nodes.tool_exec import _round_delivery
        from app.agent.graph import _route_after_tool

        check("M1 中间文件不算交付", not _round_delivery(0, [], ["run_script"]), "run_script stdout-only 应 False")
        check("M1 产出增加算交付", _round_delivery(0, [{"type": "file"}], ["run_script"]), "产出物新增应 True")
        check("M1 交付工具调用算交付", _round_delivery(0, [], ["html_report"]), "html_report 调用应 True")
        check("M1 多工具含交付算交付", _round_delivery(0, [], ["run_script", "file_parse", "doc_export"]), "含 doc_export 应 True")
        # 阶梯路由（v2）：门禁不再走路由弹卡——卡点收尾由 tool_exec 置 force_final 后走 chat；
        # 路由层对 no_delivery_streak 无感（streak=3/8 均继续循环）
        check("M1 门禁路由无 confirm（v2 卡点收尾在 tool_exec）",
              _route_after_tool({"no_delivery_streak": 8, "tool_round_count": 8}) == "agent_llm",
              "streak>=8 不再路由 confirm")
        check("M1 注记阶段不打断",
              _route_after_tool({"no_delivery_streak": 3, "tool_round_count": 3}) == "agent_llm", "streak=3 应继续")
        # 2026-08-17：v2 已放宽 4→5（见项目文档 2026-08-14 修订），断言同步
        check("M1 连续 5 轮无输出兜底",
              _route_after_tool({"no_progress_streak": 5, "tool_round_count": 5}) == "chat", "streak>=5 收尾")
        check("M1 硬上限兜底不变",
              _route_after_tool({"no_progress_streak": 0, "tool_round_count": 40}) == "chat", "cap=40 无 summary 收尾")

        # D8/D10/E-10/B8：persist_round（mock final_state 含草稿+终稿+图表+计划）
        round_id = await _next_round(engine, sid)
        chart_id = str(uuid.uuid4())
        final_state = {
            "messages": [
                {"role": "user", "content": "旧"},  # 不会落（persist 单独插 user）
                {"role": "assistant", "content": "这是草稿不应落库", "tool_calls": None},
                {"role": "assistant", "content": "草稿A（无 tool_calls 但非最后）"},
                {"role": "assistant", "content": "终稿B（最后一条）"},
            ],
            "request_user_content": "【上下文开始】ctx\n\n问题X",
            "round_outputs": [
                {"type": "chart", "label": "销售趋势", "chart_type": "line",
                 "chart_id": chart_id, "option": {"xAxis": {}}},
            ],
            "plan": "先查数据再画图",  # B8：计划轮 → pending
            "tool_events": [],
        }
        await persist_round(sid, round_id, "问题X", final_state, [])
        from sqlalchemy import text as _t2

        from app.models import ChatMessage

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    _t2("SELECT role, content, round_id FROM chat_messages "
                        "WHERE session_id=:s ORDER BY created_at"),
                    {"s": sid},
                )
            ).all()
            charts = (
                await conn.execute(
                    _t2("SELECT chart_label, round_id FROM chart_outputs WHERE session_id=:s"),
                    {"s": sid},
                )
            ).all()
            sess = (
                await conn.execute(_t2("SELECT plan_status, plan_text, plan_round_id FROM sessions WHERE id=:s"),
                                   {"s": sid})
            ).first()
        # D8：草稿+终稿只落一条（user 1 + assistant 1）
        roles = [r[0] for r in rows]
        check("D8 只落一条 assistant", roles.count("assistant") == 1 and roles.count("user") == 1,
              f"roles={roles}")
        # D10：落库 user 内容与 request_user_content 一致（含上下文块）
        user_row = [r for r in rows if r[0] == "user"][0]
        check("D10 落库含上下文块", "【上下文开始】ctx" in (user_row[1] or ""),
              f"content={str(user_row[1])[:40]}")
        # D8：assistant 内容是终稿
        asst_row = [r for r in rows if r[0] == "assistant"][0]
        check("D8 落库内容为终稿", "终稿B" in (asst_row[1] or "") and "草稿A" not in (asst_row[1] or ""),
              f"content={str(asst_row[1])[:40]}")
        # E-10：同轮图表保留
        check("E-10 同轮图表落库", len(charts) == 1 and charts[0][1] == round_id, f"charts={charts}")
        # B8：计划轮 → pending
        check("B8 计划轮写 pending", sess[0] == "pending" and sess[1] == "先查数据再画图" and sess[2] == round_id,
              f"plan_status={sess[0]}")

        # E-10：跨轮去重（新一轮同 label 图表 → 旧轮被删）
        round2 = await _next_round(engine, sid)
        final_state2 = {
            "messages": [{"role": "assistant", "content": "重画"}],
            "round_outputs": [
                {"type": "chart", "label": "销售趋势", "chart_type": "line",
                 "chart_id": str(uuid.uuid4()), "option": {"xAxis": {}}},
            ],
            "plan_confirmed": True,  # B8：确认执行轮 → 复位 none
            "tool_events": [],
        }
        await persist_round(sid, round2, "重画", final_state2, [])
        async with engine.connect() as conn:
            charts2 = (
                await conn.execute(_t2("SELECT round_id FROM chart_outputs WHERE session_id=:s"), {"s": sid})
            ).all()
            sess2 = (
                await conn.execute(_t2("SELECT plan_status, plan_text FROM sessions WHERE id=:s"), {"s": sid})
            ).first()
        check("E-10 跨轮去重（旧轮删新轮留）", len(charts2) == 1 and charts2[0][0] == round2, f"charts={charts2}")
        check("B8 确认轮复位 none", sess2[0] == "none" and sess2[1] is None, f"plan_status={sess2[0]}")

    finally:
        async with engine.begin() as conn:
            from sqlalchemy import text as _t3

            await conn.execute(_t3("DELETE FROM chat_messages WHERE session_id=:s"), {"s": sid})
            await conn.execute(_t3("DELETE FROM chart_outputs WHERE session_id=:s"), {"s": sid})
            await conn.execute(_t3("DELETE FROM sessions WHERE id=:s"), {"s": sid})


async def test_http() -> None:
    """HTTP 层：E-02/E-03/E-04/D13/D23。"""
    print("[2] HTTP 层：E-02/E-03/E-04/D13/D23")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001/api/v1", timeout=30) as c:
        token_m = await _login(c, "demo", "demo", pw("demo"))
        token_a = await _login(c, "dept_root", "admin", pw("admin"))
        check("demo 登录", token_m is not None)
        check("admin 登录", token_a is not None)
        H = {"Authorization": f"Bearer {token_m}"}

        # E-03：非 UUID → 422 E011
        r = await c.get("/chat/sessions/not-a-uuid/messages", headers=H)
        check("E-03 非 UUID 422", r.status_code == 422 and r.json().get("error", {}).get("code") == "E011",
              f"status={r.status_code}")

        # E-04：admin kb 详情（真实文档 id=1 或跳过——存在则查）
        r = await c.get("/admin/knowledge/documents/1/detail", headers={"Authorization": f"Bearer {token_a}"})
        # 文档 1 存在 → 200；不存在 → 404（两者都不应 500）
        check("E-04 kb 详情不 500", r.status_code in (200, 404), f"status={r.status_code} body={r.text[:80]}")

        # D13：feedback 状态机（自建自删）
        f = await c.post("/feedback", data={"content": "冒烟反馈"}, headers=H)
        fid = f.json().get("id") if f.status_code == 200 else None
        if fid:
            AH = {"Authorization": f"Bearer {token_a}"}
            # 非法流转：done → new 回退
            r = await c.put(f"/admin/feedback/{fid}", json={"status": "done", "reply": "已处理"}, headers=AH)
            check("D13 done 前置（有 reply）通过", r.status_code == 200, f"status={r.status_code} {r.text[:80]}")
            r2 = await c.put(f"/admin/feedback/{fid}", json={"status": "new"}, headers=AH)
            check("D13 done→new 回退拒绝", r2.status_code == 400, f"status={r2.status_code} {r2.text[:80]}")
            # 任意字符串 status 拒绝（Pydantic Literal → 422）
            r3 = await c.put(f"/admin/feedback/{fid}", json={"status": "garbage"}, headers=AH)
            check("D13 任意 status 422", r3.status_code == 422, f"status={r3.status_code}")
            # done 无 reply 拒绝
            r4 = await c.put("/admin/feedback/1", json={"status": "done"}, headers=AH)  # 用不存在的 id 也行——先建一条新的
            # 新建一条直接 done 无 reply
            f2 = await c.post("/feedback", data={"content": "冒烟反馈2"}, headers=H)
            fid2 = f2.json().get("id")
            r5 = await c.put(f"/admin/feedback/{fid2}", json={"status": "done"}, headers=AH)
            check("D13 done 无 reply 拒绝", r5.status_code == 400, f"status={r5.status_code} {r5.text[:80]}")
            # 清理
            import sqlalchemy as sa

            from app.core.database import get_global_engine

            engine = get_global_engine()
            async with engine.begin() as conn:
                await conn.execute(sa.text("DELETE FROM feedback WHERE id IN (:a, :b)"), {"a": fid, "b": fid2})

        # D23：kb 下架（自建文档再下架）
        kb = await c.post(
            "/knowledge/documents",
            files={"file": ("冒烟测试.md", "# 冒烟\n内容：这是一篇仅用于测试的文档".encode("utf-8"), "text/markdown")},
            headers=H,
        )
        if kb.status_code == 200:
            doc_id = kb.json().get("id")
            # 检索可见
            r = await c.get("/knowledge/search", params={"q": "冒烟测试"}, headers=H)
            visible = any(d.get("id") == doc_id for d in r.json().get("documents", []))
            # 下架（dept_admin 或 admin——demo 是 employee 无权，用 admin）
            r2 = await c.put(f"/knowledge/documents/{doc_id}/status", json={"status": "archived"},
                             headers={"Authorization": f"Bearer {token_a}"})
            check("D23 下架接口", r2.status_code == 200, f"status={r2.status_code} {r2.text[:80]}")
            r3 = await c.get("/knowledge/search", params={"q": "冒烟测试"}, headers=H)
            invisible = not any(d.get("id") == doc_id for d in r3.json().get("documents", []))
            check("D23 下架后检索不可见", invisible, "下架后仍可见")
            # 恢复 + 清理（employee 上传者自删：E-13）
            await c.put(f"/knowledge/documents/{doc_id}/status", json={"status": "active"},
                        headers={"Authorization": f"Bearer {token_a}"})
            r4 = await c.delete(f"/knowledge/documents/{doc_id}", headers=H)
            check("E-13 上传者自删", r4.status_code == 200, f"status={r4.status_code} {r4.text[:80]}")


async def test_path_traversal() -> None:
    """E-02：raw socket 路径穿越（不经过 httpx/curl 的 URL 规范化）。"""
    print("[3] 安全：E-02 路径穿越")
    import socket as _socket

    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001/api/v1") as c:
        token = await _login(c, "demo", "demo", pw("demo"))

    def raw_get(path: str) -> str:
        s = _socket.create_connection(("127.0.0.1", 8000), timeout=10)
        req = (f"GET /api/v1{path} HTTP/1.1\r\nHost: 127.0.0.1:8001\r\n"
               f"Cookie: access_token={token}\r\nConnection: close\r\n\r\n").encode()
        s.sendall(req)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        head, _, body = data.partition(b"\r\n\r\n")
        return head.split(b" ")[1].decode(), body[:80].decode(errors="replace")

    for path in (
        "/outputs/11111111-1111-1111-1111-111111111111/1/..%2F..%2F..%2Fetc%2Fpasswd",
        "/outputs/11111111-1111-1111-1111-111111111111/1/..%2F..%2F..%2Fopt%2Ftardis%2Fbackend%2F.env",
    ):
        status, body = raw_get(path)
        check(f"E-02 穿越拒绝（{path[-20:]}）", status in ("403", "404"), f"status={status} body={body[:40]}")


async def main() -> None:
    print("== design_fix_smoke（2026-08-10 设计检查修复验收）==")
    await test_services()
    await test_http()
    await test_path_traversal()
    print(f"\n结果：PASS {PASS} / FAIL {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
