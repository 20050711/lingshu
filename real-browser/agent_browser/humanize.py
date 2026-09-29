"""拟人行为层：对数正态延迟 + 贝塞尔鼠标 + 逐键键入 + **路径策略**。

参数表移植自 stealth-browser（与 go 侧同源）。

三条本项目的补充规则：
1. **落点不由我们按坐标按下去**：移动结束后交给 Playwright 的 `locator.click()`
   完成最后一步——它会重新测量、做命中检查、必要时重试。
   原因（2026-09-24 定）：拟人移动是"慢动作"，移动期间页面可能因 hover/懒加载重排，
   按旧坐标按下就会点歪——把最后一步交给带 actionability 检查的通道是免费的保险。
2. **路径策略**（`path`）：hover 会触发副作用的页面（下拉、带预览的卡片）用 `direct`/`instant`
   减少掠过；普通元素用 `curve` 保真。
3. **不做无原因的随机化**：所有随机都来自参数表，不额外加"抖动表演"。
"""
from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class LogNormal:
    mu: float
    sigma: float
    min_s: float
    max_s: float

    def sample(self) -> float:
        v = math.exp(self.mu + self.sigma * random.gauss(0, 1))
        return min(max(v, self.min_s), self.max_s)


# 与 go humanize/provider.go 的 DefaultProvider 同源（11 档）
TIMING: dict[str, LogNormal] = {
    "after_click":    LogNormal(-0.92, 0.35, 0.150, 2.0),
    "after_type":     LogNormal(-0.51, 0.40, 0.200, 3.0),
    "after_navigate": LogNormal(0.41, 0.45, 0.600, 6.0),
    "between_scroll": LogNormal(-0.22, 0.40, 0.250, 3.0),
    "before_submit":  LogNormal(0.0, 0.40, 0.400, 4.0),
    "before_click":   LogNormal(-1.61, 0.45, 0.080, 1.0),
    "reading":        LogNormal(-0.36, 0.40, 0.300, 3.0),
    "keystroke":      LogNormal(-2.12, 0.50, 0.030, 0.400),
    "click_hold":     LogNormal(-2.47, 0.33, 0.045, 0.250),
    "after_interact": LogNormal(0.88, 0.30, 2.0, 3.0),
    "pointer_settle": LogNormal(-1.20, 0.35, 0.200, 1.200),
}


async def delay(action: str) -> None:
    await asyncio.sleep(TIMING[action].sample())


class Cursor:
    """Playwright 不暴露当前坐标，自己记账。"""

    def __init__(self, x: float = 260.0, y: float = 180.0):
        self.x, self.y = x, y


def _ease_in_out(t: float) -> float:
    return 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2


def _jitter(size: float) -> float:
    limit = min(abs(size) * 0.15, 8)
    return (random.random() - 0.5) * 2 * limit


async def _smooth_steps(page, cursor: Cursor, tx: float, ty: float, steps: int) -> None:
    """两段式直线（带 ease）——用于 direct 路径：先竖直、后水平，各一小段。"""
    sx, sy = cursor.x, cursor.y
    for i in range(1, steps + 1):
        t = _ease_in_out(i / steps)
        await page.mouse.move(sx + (tx - sx) * t, sy + (ty - sy) * t)
        await asyncio.sleep(random.uniform(0.005, 0.009))
    cursor.x, cursor.y = tx, ty


async def move_curved(page, cursor: Cursor, tx: float, ty: float) -> None:
    """贝塞尔曲线移动（默认路径：像真人的手）。"""
    sx, sy = cursor.x, cursor.y
    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy)
    if dist < 6:
        await page.mouse.move(tx, ty)
        cursor.x, cursor.y = tx, ty
        return

    steps = min(max(round(dist / 10), 10), 40)
    nx, ny = -dy / dist, dx / dist
    off = dist * (0.05 + random.random() * 0.10)
    if random.randint(0, 1) == 0:
        off = -off
    c1x, c1y = sx + dx / 3 + nx * off, sy + dy / 3 + ny * off
    c2x, c2y = sx + dx * 2 / 3 + nx * off * 0.5, sy + dy * 2 / 3 + ny * off * 0.5

    for i in range(1, steps + 1):
        t = _ease_in_out(i / steps)
        u = 1 - t
        x = u**3 * sx + 3 * u**2 * t * c1x + 3 * u * t**2 * c2x + t**3 * tx
        y = u**3 * sy + 3 * u**2 * t * c1y + 3 * u * t**2 * c2y + t**3 * ty
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.005, 0.009))
    cursor.x, cursor.y = tx, ty


async def move_direct(page, cursor: Cursor, tx: float, ty: float) -> None:
    """直线两段（先纵后横）：给"hover 有副作用"的元素用的低掠过路径。

    不是"不拟人"——仍然有速度剖面与步进间隔；只是**少掠过元素**。
    """
    sx, sy = cursor.x, cursor.y
    seg1 = max(2, min(round(abs(ty - sy) / 14), 24))
    await _smooth_steps(page, cursor, sx, ty, seg1)
    seg2 = max(2, min(round(abs(tx - sx) / 14), 24))
    await _smooth_steps(page, cursor, tx, ty, seg2)


async def _target_point(locator) -> tuple[float, float, float, float]:
    await locator.scroll_into_view_if_needed(timeout=5000)
    box = await locator.bounding_box()
    if not box:
        raise RuntimeError("元素无可见区域（bounding_box 为空）")
    cx = box["x"] + box["width"] / 2 + _jitter(box["width"])
    cy = box["y"] + box["height"] / 2 + _jitter(box["height"])
    return cx, cy, box["width"], box["height"]


async def click(page, locator, cursor: Cursor | None = None, *,
                path: str = "curve", after: bool = True) -> Cursor:
    """拟人点击。

    path:
      - ``curve``（默认）：贝塞尔曲线，最像真人
      - ``direct``：先纵后横两段直线，少掠过其它元素（用于 hover 有副作用的元素）
      - ``instant``：不做前置移动，直接交给 Playwright（靶点明确、且绝不能 hover 时用）
    """
    cursor = cursor or Cursor()
    await delay("before_click")

    if path != "instant":
        tx, ty, _, _ = await _target_point(locator)
        if path == "direct":
            await move_direct(page, cursor, tx, ty)
        else:
            await move_curved(page, cursor, tx, ty)
        await delay("pointer_settle")

    # 最后一步交给 Playwright：重新测量 + 命中检查 + 必要时重试（防"移动期间页面重排→点歪"）
    hold_ms = int(TIMING["click_hold"].sample() * 1000)
    await locator.click(delay=hold_ms, timeout=8000)
    if after:
        await delay("after_click")
    return cursor


async def type_text(page, locator, text: str, *, click_first: bool = True,
                    submit: bool = False, cursor: Cursor | None = None,
                    path: str = "curve") -> Cursor:
    """逐键键入（禁 fill/直赋值）。

    实测口径：``keyboard.type`` 对 ASCII 产生完整事件链；对 CJK 走 insertText
    （近真实输入法，但无 composition 事件——站点若深查输入法痕迹，此处是已知短板，
    因此**优先使用站点自带的话术模板/常用语**，别让 agent 生成中文长文本再键入）。
    """
    cursor = cursor or Cursor()
    if click_first:
        cursor = await click(page, locator, cursor, path=path, after=False)
    else:
        await locator.focus()

    since_pause = 0
    for ch in text:
        await page.keyboard.type(ch)
        await delay("keystroke")
        since_pause += 1
        if ch in " ，。！？,.!?、；;：:":
            await asyncio.sleep(TIMING["keystroke"].sample() * random.uniform(1.5, 3.0))
        elif since_pause >= 8 and random.random() < 0.25:
            await asyncio.sleep(TIMING["reading"].sample() * 0.5)
            since_pause = 0
    if submit:
        await delay("before_submit")
        await page.keyboard.press("Enter")
    await delay("after_type")
    return cursor


async def scroll(page, dy: int) -> None:
    """拟人滚动：拆成多段滚轮 + 段间停顿 + 偶发阅读停顿。"""
    remaining = abs(dy)
    sign = 1 if dy > 0 else -1
    while remaining > 0:
        step = min(random.randint(80, 240), remaining)
        await page.mouse.wheel(0, sign * step)
        remaining -= step
        if remaining <= 0:
            break
        await delay("between_scroll")
        if random.random() < 0.2:
            await delay("reading")
