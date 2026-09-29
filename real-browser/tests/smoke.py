"""本地冒烟：不开外网也能验全链路。

覆盖：开页 → 快照（编号 + 邻近上下文 + 下拉标注）→ 点击（含状态摘要）→ 输入 → 隔离世界读取
→ 截图 → 优雅关闭；并在 Windows 上顺带核对"浏览器进程没有任何 LISTENING 端口"。

跑法（Windows 侧，用已有 venv）：
    E:\\stealth-browser-win\\.venv\\Scripts\\python.exe <repo>\\tests\\smoke.py
"""
from __future__ import annotations

import asyncio
import http.server
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 控制台默认 GBK，中文/符号会炸——统一切 UTF-8 输出
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent_browser.browser_agent import BrowserAgent  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures"
PORT = 8791


def serve() -> socketserver.TCPServer:
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(FIXTURE), **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def check_no_ports() -> str:
    """Windows：确认浏览器进程没有监听端口（R1：绝不开调试端口）。"""
    import os
    import subprocess
    if os.name != "nt":
        return "（非 Windows，跳过端口核对）"
    exe = "msedge.exe" if os.environ.get("AB_TEST_EDGE", "1") == "1" else "chrome.exe"
    try:
        tl = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}",
                             "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        pids = set()
        for line in tl.splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) >= 2 and cols[1].isdigit():
                pids.add(cols[1])
        if not pids:
            return "（没找到浏览器进程）"
        ns = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
        hits = [l.split()[1] for l in ns.splitlines()
                if "LISTENING" in l and l.split() and l.split()[-1] in pids]
        return f"监听端口：{hits}" if hits else "无监听端口 ✓（R1 成立）"
    except Exception as e:
        return f"（端口核对失败：{e}）"


async def main() -> int:
    httpd = serve()
    ok = True
    agent = BrowserAgent("smoke_test", site="default", allow_local=True)
    try:
        print("① 打开夹具页 …")
        text = await agent.open(f"http://127.0.0.1:{PORT}/selftest_page.html")
        assert "URL:" in text and "[1]" in text, text[:300]
        assert "可交互：" in text
        print("  ✓ 快照带编号与计数")
        assert "←" in text and "丙" in text, "邻近上下文缺失：\n" + text[:600]
        print("  ✓ 邻近上下文（按钮 ← 卡片文本）")

        print("  · 下拉被单独标注：", any("下拉" in l for l in text.splitlines()))

        def ref_of(blob: str, *needles: str) -> int | None:
            for line in blob.splitlines():
                if line.startswith("[") and all(n in line for n in needles):
                    return int(line.split("]")[0].strip("["))
            return None

        print("② 点列表里第 2 张卡的按钮 …")
        ref = ref_of(text, "按钮", "打招呼", "乙")
        assert ref, "没找到目标按钮 ref：\n" + text[:800]
        out = await agent.click(ref)
        assert "状态：" in out, out[:300]
        print("  ✓ 点击返回状态摘要：", out.splitlines()[1][:80])

        print("③ 输入框逐键输入 …")
        ref_input = ref_of(text, "输入框")
        assert ref_input, "没找到输入框"
        out = await agent.type_text(ref_input, "zhang ming 28")
        print("  ✓", out)

        print("④ 隔离世界读取（browse_eval 的底座）…")
        val = await agent.eval_read("document.querySelectorAll('.card').length")
        assert val.strip() == "6", f"期望 6 张卡，得到 {val!r}"
        print("  ✓ 隔离世界读到卡片数 =", val.strip())

        print("⑤ 结构化读取（browse_extract）…")
        data = await agent.extract("JSON.stringify([...document.querySelectorAll('.card h3')].map(e => e.textContent))")
        assert "甲" in data and "乙" in data, data[:200]
        print("  ✓ extract 正常：", data.replace("\n", " ")[:80])

        print("⑥ 无 href 的可点元素也有编号（JS 绑定点击的按钮）…")
        ref_chat = ref_of(text, "可点", "立即沟通")
        assert ref_chat, "无 href 的 <a> 没被编号——SPA 上最常见的可点元素会点不到：\n" + text[:900]
        print(f"  ✓ 编号 [{ref_chat}] 可点 “立即沟通”")

        print("⑦ 用 CSS 选择器直接点（快照没编号时的兜底）…")
        out = await agent.click(selector="#chat-btn")
        assert "状态：" in out, out[:300]
        got = (await agent.eval_read("document.getElementById('out').textContent")).strip()
        assert "已投递：丁" in got, f"点了但页面没变：{got!r}"
        print(f"  ✓ 选择器点击生效，页面变成 {got!r}")

        print("⑧ contenteditable 被当成输入框列出（现代聊天框）…")
        ref_box = ref_of(text, "输入框", "可编辑区")
        assert ref_box, "contenteditable 没被列出：\n" + text[:900]
        print(f"  ✓ 编号 [{ref_box}] 可编辑区")

        print("⑨ browse_wait：等目标文字出现（导航返回 ≠ 内容就绪）…")
        waited = await agent.wait_for("立即沟通", timeout_sec=5)
        assert waited.startswith("✓"), waited[:200]
        print("  ✓", waited.splitlines()[0])

        print("⑩ 点 target=_blank 的链接：应跟到新标签页…")
        snap_now = await agent.snapshot()                      # 重新取号，别用旧编号
        ref_new = ref_of(snap_now, "链接", "在新标签打开详情")
        assert ref_new, "没找到新标签链接：\n" + snap_now[:900]
        out = await agent.click(ref_new)
        assert "新标签" in out and "from=card-e" in out, f"没跟随新标签页：{out[:400]}"
        print("  ✓", [ln for ln in out.splitlines() if "新标签" in ln][0][:90])

        print("⑪ 截图 …")
        shot = await agent.screenshot()
        print("  ✓", shot)

        print("⑫ 端口核对 …")
        print("  ", check_no_ports())
    except AssertionError as e:
        ok = False
        print("✗ 断言失败：", e)
    except Exception as e:
        ok = False
        print("✗ 异常：", type(e).__name__, e)
    finally:
        try:
            print("⑬ 优雅关闭 …")
            print("  ", await agent.close())
        except Exception as e:
            ok = False
            print("✗ 关闭异常：", e)
        httpd.shutdown()
    print("\n结论：", "全部通过 ✓" if ok else "有失败 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
