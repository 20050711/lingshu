"""M5b 工具冒烟测试-短套件（2026-08-21 拆分）：doc_export / file_search / run_script 全系列。

本地/快速项一起跑（秒级~30s）。长任务（web_search 联网搜索 / image_generation GLM 图片
生成——真实网络调用，最长可达 120-150s）已拆到 tools_net_smoke.py 单独跑：
    python tests/tools_smoke.py      # 短套件
    python tests/tools_net_smoke.py  # 长套件（网络/LLM）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.tools import ToolContext, get_tool

CTX = ToolContext("smoke-sess", 1, "demo", "employee", "smoke", "/data/outputs/smoke/1", 2)  # 2026-09-01：加 user_id=2（demo 员工）——原默认 0 命中 mock 期空缓存 kb_match 失败


async def main() -> int:
    # 2026-09-02：进程内冒烟确定性——mock LLM（kb_match 关键词提取走软观察；真实档验证由
    # 免费档回归/工具 e2e 覆盖），避免测试进程 settings 与服务端不一致时真实调用/结果抖动
    from app.core.config import get_settings as _gs

    _gs().llm_mock = True
    results = []

    # 1. doc_export docx
    t = get_tool("doc_export")
    r = await t.handler({"format": "docx", "title": "周报测试", "sections": [{"heading": "本周销售", "content": "销售额 17132 元\n转化率 6.9%"}]}, CTX)
    ok = "file_path" in r and not isinstance(r, dict) or (isinstance(r, dict) and "file_path" in r)
    results.append(("doc_export docx", ok and not (isinstance(r, dict) and "error" in r), r.get("file_path") if isinstance(r, dict) else str(r)))

    # 2. doc_export xlsx
    r = await t.handler({"format": "xlsx", "title": "数据", "table": {"headers": ["区域", "销售额"], "rows": [["华东", 7792], ["华北", 6795]]}}, CTX)
    results.append(("doc_export xlsx", "file_path" in r, r.get("file_path")))

    # 3. file_search（2026-09-10：kb_match 下线，知识库统一检索入口）
    # mock 档软观察：llm_mock 下辅助 client 无关键词提取能力（返回非 JSON → 回退整句 → 预筛可能不命中）
    # 无 query 的「列清单」模式不依赖 LLM，任何档都必须通过——用它做硬断言
    from app.core.config import get_settings as _get_settings

    t = get_tool("file_search")
    r = await t.handler({"query": "周报什么时候提交？"}, CTX)
    ok_search = isinstance(r, dict) and ("content_matches" in r or "error" in r)
    ok_hits = isinstance(r, dict) and bool(r.get("content_matches"))
    if _get_settings().llm_mock and not ok_hits:
        results.append(("file_search 检索（mock 软观察）", ok_search,
                        "mock 辅助 client 无关键词提取能力，真实档验证命中"))
    else:
        results.append(("file_search 检索（跨库内容块）", ok_hits,
                        [m.get("title") or m.get("file_name") for m in (r.get("content_matches") or [])][:3]
                        if isinstance(r, dict) else r))

    r = await t.handler({}, CTX)
    ok_inv = isinstance(r, dict) and r.get("mode") == "inventory" and "folder_matches" in r
    results.append(("file_search 列清单（无 query，不依赖 LLM）", ok_inv,
                    f"folders={len(r.get('folder_matches') or [])} docs={len(r.get('documents') or [])}"
                    if isinstance(r, dict) else r))

    # 4-5. web_search / image_generation 已拆至 tools_net_smoke.py（2026-08-21：长任务单独跑）

    # 6. run_script 沙箱（三期 M17：正常 + 超时）
    t = get_tool("run_script")
    r = await t.handler({"code": "print(sum(range(1, 101)))"}, CTX)
    ok = isinstance(r, dict) and r.get("stdout", "").strip() == "5050"
    results.append(("run_script 正常", ok, r.get("stdout", "").strip()[:40] if isinstance(r, dict) else r))
    r2 = await t.handler({"code": "while True: pass"}, CTX)
    ok2 = isinstance(r2, dict) and "超时" in (r2.get("error") or "")
    results.append(("run_script 超时终止", ok2, r2.get("error", "")[:40] if isinstance(r2, dict) else r2))

    # 7. run_script 四态（2026-08-10 A1 沙盒四态 + T3 预览）
    from app.core.config import get_settings

    _cfg = get_settings()
    r = await t.handler({"mode": "write", "file": "hello.txt", "code": "print('hi')\n# 第二行"}, CTX)
    ok = isinstance(r, dict) and r.get("work_files") and any(f["name"] == "hello.txt" for f in r["work_files"])
    has_preview = isinstance(r, dict) and any("preview" in f for f in r.get("work_files", []))
    results.append(("run_script write+预览(T3)", ok and has_preview,
                    [f["name"] for f in r.get("work_files", [])] if isinstance(r, dict) else r))
    # 2026-08-10：write 大文件（原 20K 硬编码上限——DeepSeek 384K 输出上限下中小型 HTML 可直接写入）
    big = await t.handler({"mode": "write", "file": "big.html", "code": "<html>" + ("x" * 50000) + "</html>"}, CTX)
    ok = isinstance(big, dict) and "已写入 big.html" in big.get("stdout", "")
    results.append(("run_script write 大文件(50K)", ok, big.get("stdout", "")[:40] if isinstance(big, dict) else big))
    r = await t.handler({"mode": "run", "file": "hello.txt"}, CTX)
    ok = isinstance(r, dict) and "hi" in r.get("stdout", "")
    results.append(("run_script run file= 复用", ok, r.get("stdout", "").strip()[:40] if isinstance(r, dict) else r))
    r = await t.handler({"mode": "edit", "file": "hello.txt", "old_text": "print('hi')", "new_text": "print('hello')"}, CTX)
    ok = isinstance(r, dict) and "替换 1 处" in r.get("stdout", "")
    results.append(("run_script edit 唯一替换", ok, r.get("stdout", "")[:40] if isinstance(r, dict) else r))
    # edit 不唯一用独立文件（不污染 hello.txt——后续 grep 依赖其内容）
    await t.handler({"mode": "write", "file": "dup.txt", "code": "aaa"}, CTX)
    r = await t.handler({"mode": "edit", "file": "dup.txt", "old_text": "a", "new_text": "b"}, CTX)
    ok = isinstance(r, dict) and "不唯一" in r.get("error", "")
    results.append(("run_script edit 不唯一报错", ok, r.get("error", "")[:40] if isinstance(r, dict) else r))
    r = await t.handler({"mode": "grep", "pattern": "hello", "file": "hello.txt"}, CTX)
    ok = isinstance(r, dict) and "hello.txt:1" in r.get("stdout", "")
    results.append(("run_script grep 命中", ok, r.get("stdout", "").strip()[:60] if isinstance(r, dict) else r))

    # 8. T2 stdout 降权：大输出预截断 ≤ 配置上限（21 轮事故：完整 stdout 挤爆历史预算）
    r = await t.handler({"mode": "run", "code": "print('x' * 200000)"}, CTX)
    cap = _cfg.sandbox_stdout_cap_chars
    ok = isinstance(r, dict) and len(r.get("stdout", "")) <= cap and "已截断" in r.get("stdout", "")
    results.append(("run_script stdout 降权(T2)", ok,
                    f"len={len(r.get('stdout', ''))} cap={cap}" if isinstance(r, dict) else r))

    # 9. T4 deliver：work 文件发布为交付物（host 拷贝到产出目录 + file_path）
    r = await t.handler({"mode": "deliver", "file": "hello.txt", "label": "交付测试"}, CTX)
    ok = isinstance(r, dict) and bool(r.get("file_path")) and "outputs" in (r.get("file_path") or "")
    if ok and isinstance(r, dict):
        fname = r["file_path"].rsplit("/", 1)[-1]
        ok = (Path("/data/outputs/smoke/1") / fname).exists()
    results.append(("run_script deliver 交付(T4)", ok, r.get("file_path") if isinstance(r, dict) else r))
    r = await t.handler({"mode": "deliver", "file": "no_such.txt"}, CTX)
    ok = isinstance(r, dict) and "没有" in r.get("error", "")
    results.append(("run_script deliver 缺失文件报错", ok, r.get("error", "")[:40] if isinstance(r, dict) else r))

    for name, ok, info in results:
        print(f"  {'✓' if ok else '✗'} {name}: {info}")
    all_ok = all(ok for _, ok, _ in results)
    print("=== PASS ===" if all_ok else "=== FAIL ===")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
