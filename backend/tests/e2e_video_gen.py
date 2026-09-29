"""E2E：video_generate 文生视频全链路（Agnes 异步任务 → 轮询 → 下载转存）。

- 经部署形态 :24426 建会话（产出归属）
- 直接调用 video_generate 工具（进程内，避免 LLM 是否调用工具的不可控性）
- 断言：返回 output_type=video + file_path + 本地文件落盘存在
- 注意：真实调用 Agnes 视频服务（3 秒视频 ≈ 3s 日配额，每日 500s），可能 503 队列满退避

用法: cd backend && source scripts/env_aip.sh && python -u tests/e2e_video_gen.py
"""
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:24426/api/v1"
PROMPT = "夜空下平静的湖面，星光倒影，缓慢推镜头"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'✓' if ok else '✗'} {name} {detail}")


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        r = await c.post("/auth/login", json={"department_id": "press", "username": "press13", "password": "Press#2026$Test"})
        r.raise_for_status()
        h = {"Authorization": f"Bearer {r.cookies['access_token']}"}
        sid = (await c.post("/chat/sessions", headers=h, json={"client_id": "e2e-vg"})).json()["session_id"]
        print(f"session: {sid}")

        from app.agent.tools import ToolContext, get_tool

        ctx = ToolContext(sid, 1, "press", "employee", "e2e-vg", f"/data/outputs/{sid}/1")
        print("== 调用 video_generate（3s 文生视频，轮询 Agnes）==")
        result = await get_tool("video_generate").handler(
            {"prompt": PROMPT, "duration": 3, "mode": "text2video"}, ctx
        )
        print("工具返回:", {k: v for k, v in result.items() if k != "error"})

        if "error" in result:
            check("video_generate 无错误", False, f"error={result['error'][:150]}")
        else:
            check("返回 output_type=video", result.get("output_type") == "video", f"output_type={result.get('output_type')}")
            fp = result.get("file_path", "")
            check("返回 file_path", bool(fp), fp[:80])
            # 本地落盘验证（/api/v1/outputs/{sid}/{round}/{fname}）
            fname = fp.rsplit("/", 1)[-1]
            local = Path(f"/data/outputs/{sid}/1/{fname}")
            check("本地文件落盘", local.exists() and local.stat().st_size > 0,
                  f"{local.name} {local.stat().st_size if local.exists() else 0} bytes")
            if local.exists():
                # 鉴权下载（部署形态全链路）
                dl = await c.get(f"/outputs/{sid}/1/{fname}", headers=h)
                check("鉴权下载 200", dl.status_code == 200, f"status={dl.status_code}")

    ok = all(ok for _, ok, _ in results)
    print("=== PASS ===" if ok else "=== FAIL ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
