"""工具冒烟测试-长套件（2026-08-21 拆分）：web_search / image_generation（真实网络调用）。

从 tools_smoke.py 拆出——这两个调用走公网 API（联网搜索 90s 上限 / GLM 图片生成 120s 上限），
单独跑避免拖慢短套件（doc_export/run_script 等秒级项）。
用法: python tests/tools_net_smoke.py [--skip-glm]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.tools import ToolContext, get_tool

CTX = ToolContext("smoke-sess", 1, "demo", "employee", "smoke", "/data/outputs/smoke/1")


async def main() -> int:
    skip_glm = "--skip-glm" in sys.argv
    results = []

    # 1. web_search（可降级：失败不阻塞，但 error 视为未通过）
    t = get_tool("web_search")
    r = await t.handler({"query": "AI 广告渠道行业动态"}, CTX)
    ok = not (isinstance(r, dict) and "error" in r)
    results.append(("web_search", ok, (str(r.get("results", ""))[:40] if isinstance(r, dict) else str(r))[:60]))

    # 2. GLM 图片生成（验证 key；--skip-glm 跳过——本地无 key 时用）
    if not skip_glm:
        t = get_tool("image_generation")
        r = await t.handler({"prompt": "一张简约的企业数据看板海报", "size": "1024x1024", "n": 1}, CTX)
        ok = isinstance(r, dict) and r.get("images")
        results.append(("image_generation GLM", ok, (r.get("images", [])[:1] if isinstance(r, dict) else r)))

    for name, ok, info in results:
        print(f"  {'✓' if ok else '✗'} {name}: {info}")
    all_ok = all(ok for _, ok, _ in results)
    print("=== PASS ===" if all_ok else "=== FAIL ===")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
