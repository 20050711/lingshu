"""E2E：反馈系统全链路（提交带截图 → 我的列表 → 撤销）。

- 经部署形态 :24426
- 截图 /tmp/test_photo.jpg（≤5MB，≤4 张）

用法: cd backend && source scripts/env_aip.sh && python -u tests/e2e_feedback.py
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:24426/api/v1"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name} {detail}")


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        r = await c.post("/auth/login", json={"department_id": "press", "username": "press12", "password": "Press#2026$Test"})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.cookies['access_token']}"}

        # 1. 提交反馈（带截图）
        with open("/tmp/test_photo.jpg", "rb") as f:
            r = await c.post("/feedback", headers=h, data={
                "feedback_type": "功能建议", "page": "并发测试页", "content": "e2e_feedback 自动化提交，验证反馈链路",
                "contact": "press12",
            }, files={"screenshots": ("shot.jpg", f)})
        check("提交反馈 200", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")

        # 2. 我的反馈列表含刚提交记录（提交响应无 id，从列表取最新一条）
        mine = (await c.get("/feedback/my", headers=h)).json()
        items = mine.get("items") or []
        fid = items[0]["id"] if items else None
        found = items and items[0]["content"] == "e2e_feedback 自动化提交，验证反馈链路"
        check("我的列表可见", bool(found), f"total={len(items)}")
        check("返回反馈 id", fid is not None, f"id={fid}")

        # 3. 撤销
        r = await c.post(f"/feedback/{fid}/revoke", headers=h)
        check("撤销 200", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")

    ok = all(ok for _, ok, _ in results)
    print("=== PASS ===" if ok else "=== FAIL ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
