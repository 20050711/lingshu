"""页面 → 编号快照（agent 的"眼睛"）。

**怎么取**：在**隔离世界**里跑一次异步 JS，把"稳定等待 + 候选元素 + 文本 + 邻近上下文 + 正文"
一次算完带回来（`evaluate.eval_isolated`，与 `browse_eval` 同一条通道——页面看不见、不可检测）。

> 2026-09-24 实测后改的：旧实现用 locator 逐元素取（每个元素 4~6 次 CDP 往返），
> 重页面上 **80 个元素要 29 秒、200 个要 78 秒**（每元素约 0.36s）。改成一次求值后降到秒级以内。
> 这是 §"实现口径"的唯一一次修订——原来的"全程 locator、不跑页面内 JS"在重站点上不可用。

**三张增强**：
1. **邻近上下文**：按钮/链接后面附一句"它属于哪张卡片"（否则一排"打招呼"按钮分不清谁是谁）；
2. **状态类元素标注**：下拉/组合框单独成类——点击层据此自动走低掠过路径（见 humanize.path）；
3. **稳定等待 + 截断提示**：动态渲染页面先等元素总数稳定再扫；到上限时明说"已截断"。

**已知边界**：编号只覆盖"选择器能命中的元素"。快照里没有、又确实点得到的元素，
用 ``browse_click(selector="…")`` 兜底——别为了点它去绕页面。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 元素类别 → 选择器（顺序即编号顺序）。各类之间**互斥**，避免同一元素占两个号。
KINDS: list[tuple[str, str]] = [
    ("链接", "a[href]"),
    ("按钮", "button:not([aria-haspopup]), [role=button]:not([aria-haspopup]), "
             "input[type=submit], input[type=button]"),
    # 2026-09-24 实测补：动态渲染站点里大量可点元素是**无 href 的 <a>**，或带 onclick 的 div
    # （典型形态：`<a class="...">` 无 href，靠 JS 绑定点击）。
    ("可点", "a:not([href]), "
             "[onclick]:not(a):not(button):not([role=button]):not([aria-haspopup]), "
             "[role=link]:not(a)"),
    ("下拉", "select, [role=combobox], [aria-haspopup]"),
    # contenteditable：现代聊天框常不在 input/textarea 里，而是可编辑 div
    ("输入框", "input:not([type=submit]):not([type=button]), textarea, "
               "[contenteditable]:not([contenteditable=false])"),
]

MAX_ELEMENTS = 200      # 80 在列表页会被"导航链接 + 卡片链接"吃满，右侧操作区永远排不上（实测）
SETTLE_MAX_MS = 1200    # 扫之前等"元素总数稳定"的上限；动态渲染页面必须等
SETTLE_STEP_MS = 150
BODY_CHARS = 2500
CTX_CHARS = 44
JS_TIMEOUT_MS = 15000   # 一次求值要覆盖"稳定等待 + 扫描 + 正文"，比默认 5s 放宽

# 一次求值干完所有活：返回 {items, totals, truncated, body, url, title}
_SCAN_JS = """(async () => {
  const KINDS = %(kinds)s, LIMIT = %(limit)d, CTX = %(ctx)d, BODY = %(body)d;
  const SETTLE_MAX = %(settle_max)d, STEP = %(step)d;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const labelOf = (el, kind) => {
    if (kind === '\\u8f93\\u5165\\u6846') {                    // 输入框
      let t = el.getAttribute('type') || 'text';
      if (el.hasAttribute('contenteditable')) t = '\\u53ef\\u7f16\\u8f91\\u533a';   // 可编辑区
      const hint = el.getAttribute('placeholder') || el.getAttribute('aria-label')
                || el.getAttribute('name') || '';
      return ('[' + t + '] ' + hint).trim().slice(0, 60) || null;
    }
    let txt = (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60);
    if (!txt) txt = (el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 60);
    return txt || null;
  };
  const contextOf = (el, own) => {
    const anc = el.closest('div, li, article');
    if (!anc) return null;
    let t = (anc.innerText || '').replace(/\\s+/g, ' ');
    if (own) t = t.split(own).join(' ');
    t = t.replace(/\\s+/g, ' ').trim();
    return t.slice(0, CTX) || null;
  };
  const countAll = () => KINDS.reduce((n, k) => {
    try { return n + document.querySelectorAll(k[1]).length; } catch (e) { return n; }
  }, 0);
  // 有界稳定等待：动态渲染页面"边滚边长"，扫太早会拍到半加载的页面
  const deadline = Date.now() + SETTLE_MAX;
  let last = -1, stable = 0;
  while (Date.now() < deadline) {
    const n = countAll();
    if (n === last) { if (++stable >= 2) break; } else { stable = 0; last = n; }
    await new Promise((r) => setTimeout(r, STEP));
  }
  const items = [], totals = {};
  let truncated = false, ref = 0;
  outer: for (const [kind, sel] of KINDS) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
    totals[kind] = nodes.length;
    for (let i = 0; i < nodes.length; i++) {
      const el = nodes[i];
      if (!visible(el)) continue;
      const label = labelOf(el, kind);
      if (!label) continue;                       // 无字无标签的元素对 agent 无意义
      if (ref >= LIMIT) { truncated = true; break outer; }
      items.push({k: kind, i: i, l: label, c: contextOf(el, label)});
      ref += 1;
    }
  }
  const body = ((document.body && document.body.innerText) || '').slice(0, BODY + 200);
  return {items: items, totals: totals, truncated: truncated, body: body,
          url: location.href, title: (document.title || '').slice(0, 80)};
})()"""


async def snapshot(page, limit: int = MAX_ELEMENTS, body_chars: int = BODY_CHARS) -> tuple[str, dict[int, Any]]:
    """返回 (文本快照, {ref: (locator, 类别)})。ref 只在下一次 snapshot 前有效。"""
    from .evaluate import eval_isolated          # 延迟导入，避免循环依赖

    js = _SCAN_JS % {
        "kinds": json.dumps([[k, s] for k, s in KINDS], ensure_ascii=False),
        "limit": limit, "ctx": CTX_CHARS, "body": body_chars,
        "settle_max": SETTLE_MAX_MS, "step": SETTLE_STEP_MS,
    }
    data = await eval_isolated(page, js, timeout_ms=JS_TIMEOUT_MS)
    if not isinstance(data, dict):
        raise RuntimeError(f"快照脚本没有返回预期结构（拿到 {type(data).__name__}）")

    lines: list[str] = [
        f"URL: {data.get('url') or page.url}",
        f"标题: {data.get('title') or '-'}",
    ]
    refs: dict[int, Any] = {}
    counts: dict[str, int] = {}
    sel_of = dict(KINDS)
    for n, it in enumerate(data.get("items") or [], start=1):
        kind = it.get("k", "?")
        counts[kind] = counts.get(kind, 0) + 1
        # ref 仍然是一个 **locator**：点击路径（真实鼠标 + 护栏）完全不变
        refs[n] = (page.locator(sel_of[kind]).nth(int(it.get("i", 0))), kind)
        text = it.get("l", "")
        line = f"[{n}] {kind} {text}" if kind == "输入框" else f"[{n}] {kind} “{text}”"
        if it.get("c"):
            line += f"   ← {it['c']}"
        lines.append(line)

    summary = "，".join(f"{k} {v}" for k, v in counts.items()) or "无可交互元素"
    head = f"可交互：{len(refs)} 个（{summary}）"
    if data.get("truncated"):
        total = sum(int(v) for v in (data.get("totals") or {}).values())
        head += (f"\n⚠ 已到编号上限 {limit}，页面里还匹配到约 {max(0, total - len(refs))} 个（含隐藏元素）没列出。"
                 f"没看到目标时：先 browse_scroll 再取快照；"
                 f"或直接 browse_extract 找到它的 CSS 选择器，再用 browse_click(selector=…)")
    lines.insert(2, head)

    body = re.sub(r"\n{2,}", "\n", (data.get("body") or "")).strip()
    if body:
        lines.append(f"--- 正文（截断 {body_chars} 字） ---")
        lines.append(body[:body_chars])
    return "\n".join(lines), refs


UNION_SELECTOR = ", ".join(s for _, s in KINDS)


async def count_interactive(page) -> int:
    """数"当前可交互元素"总数——**一次求值**，用于滚动/输入前后的对比。

    别用 locator 逐类 count()：那在重页面上也要几百毫秒到秒级。
    返回 -1 表示数不出来（调用方据此决定要不要显示）。
    """
    from .evaluate import eval_isolated
    try:
        n = await eval_isolated(
            page, f"document.querySelectorAll({json.dumps(UNION_SELECTOR)}).length",
            timeout_ms=4000)
        return int(n or 0)
    except Exception:
        return -1


async def count_matches(page, selector: str) -> int:
    """数某个选择器命中多少（用于"动作前后状态摘要"，如列表条数变化）。"""
    if not selector:
        return -1
    try:
        return await page.locator(selector).count()
    except Exception:
        return -1
