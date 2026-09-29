"""图表 PNG 渲染服务（二期 M8）。

方案（PLAN-v2 决策）：本地 HTML + vendored echarts.min.js 直接渲染原始 ECharts
option（自产 option 形状固定，无需 pyecharts 映射层，最保真）→ 无头浏览器截图。
2026-08-26：Selenium+Chrome → Playwright chromium（开发机/部署机均有 chromium-1234）。
并发 Semaphore(10)（对齐 report 队列并发）。

资源：echarts.min.js 落 backend/app/static/echarts.min.js；chromium 不可用时
render 抛 RuntimeError（上层降级"提示不可用 + 可复制 option JSON"）。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("services.chart_png")
_settings = get_settings()

_sem = asyncio.Semaphore(10)  # 对齐 report 队列并发 10（2026-08-11 压测调优）

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"   # backend/app/static/
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>chart</title></head>
<body style="margin:0;background:#ffffff">
<div id="c" style="width:960px;height:540px"></div>
<script src="{echarts_src}"></script>
<script>
var option = JSON.parse({option_json});
// 问题 7 修复（2026-08-17）：还原 formatter 函数字符串（chart_builder 注入的 JS 源码串
// 经 JSON 序列化后 ECharts 当模板渲染——Y 轴显示整段函数源码；仅还原 function( 开头的受控串）
(function restoreFns(node) {
  if (node && typeof node === 'object') {
    for (var k in node) {
      if (k === 'formatter' && typeof node[k] === 'string' && node[k].indexOf('function') === 0) {
        try { node[k] = eval('(' + node[k] + ')') } catch (e) {}
      } else { restoreFns(node[k]) }
    }
  }
})(option);
var chart = echarts.init(document.getElementById('c'));
chart.setOption(option);
</script>
</body></html>
"""


def chrome_available() -> bool:
    """2026-08-26：改 Playwright chromium（开发机/部署机均有 chromium-1234）——
    原 Selenium+Chrome PATH 查找在部署机不可用（未装 Chrome，PNG 导出提示不可用）。
    只做文件探测（不 launch：sync API 在事件循环内被 Playwright 拒绝，launch 由
    _snapshot 在 to_thread 线程内执行）。"""
    try:
        import glob
        import os

        import playwright  # noqa: F401 包存在性

        home = os.path.expanduser("~")
        return bool(
            glob.glob(f"{home}/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")
        )
    except Exception:
        return False


async def render_option(option: dict, out_dir: str) -> str:
    """渲染 option → PNG 落 out_dir，返回 PNG 路径。失败抛 RuntimeError。"""
    if not chrome_available():
        raise RuntimeError("Chrome 未安装，图表 PNG 导出不可用（可复制图表配置代替）")
    if not (_STATIC_DIR / "echarts.min.js").exists():
        raise RuntimeError("echarts.min.js 缺失（backend/app/static/），无法渲染 PNG")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    html = out / f"chart_{uuid.uuid4().hex[:8]}.html"
    png = out / f"chart_{uuid.uuid4().hex[:8]}.png"

    option_json = json.dumps(option, ensure_ascii=False).replace("</", "<\\/")
    html_content = (
        HTML_TEMPLATE
        .replace("{echarts_src}", f"file://{(_STATIC_DIR / 'echarts.min.js').resolve()}")
        .replace("{option_json}", json.dumps(option_json))
    )
    html.write_text(html_content, encoding="utf-8")

    async with _sem:
        await asyncio.to_thread(_snapshot, str(html), str(png))

    html.unlink(missing_ok=True)
    if not png.exists() or png.stat().st_size < 100:
        png.unlink(missing_ok=True)
        raise RuntimeError("图表 PNG 渲染失败（Chrome 截图异常）")
    return str(png)


def _snapshot(html_path: str, png_path: str) -> None:
    """同步快照：Playwright chromium 无头渲染（2026-08-26 替换 Selenium——
    两端均有 playwright chromium-1234；viewport 960×540 对齐图表容器，整页截图即图表 PNG）。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": 960, "height": 540})
            page.goto(f"file://{html_path}")
            page.wait_for_timeout(1200)  # 等待图表渲染（echarts 初始化）
            page.screenshot(path=png_path)
        finally:
            browser.close()
