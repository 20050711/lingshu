"""一体化会话：护栏 + 拟人 + 快照 + 工具实现（MCP 层与测试共用）。

工具契约（2026-09-24 定，源自另一个项目的"脏 body 入库"事故）：
1. **写动作必返新状态**——每次点击后自动附新快照，agent 想"点了就走"也做不到；
2. **关键动作带状态摘要**——URL / 标题 / 列表条数变化，对不上时人和 agent 都能一眼看出；
3. **拒绝要说话**——护栏拒绝返回"为什么 + 怎么办"，不是堆栈。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import config, humanize, notes as notes_mod, snapshot as snap_mod
from .evaluate import eval_isolated, eval_main, render
from .guard import Guard, GuardError
from .identity import load_identity
from .launcher import close as launch_close, close_user_browser, launch, resolve_final_url, \
    user_browser_running
from . import session as session_mod
from .profiles import profile_for

try:
    from patchright.async_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment,misc]

# 元素类别 → 点击路径（"下拉"会改页面状态、且常带 hover 副作用 → 低掠过路径）
KIND_PATH = {"下拉": "direct"}

# browse_wait：一次求值内部轮询，直到目标出现或到点（借鉴 web-access 的"页面就绪"判据）
_WAIT_JS = """(async () => {
  const needle = %(needle)s, isCss = %(is_css)s, timeout = %(timeout)d;
  const hit = () => {
    if (isCss) { try { return document.querySelectorAll(needle).length; } catch (e) { return -1; } }
    return (((document.body && document.body.innerText) || '').includes(needle)) ? 1 : 0;
  };
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (hit() > 0) return {found: true, ms: Date.now() - t0, count: hit() || 1,
                           url: location.href, title: document.title};
    await new Promise((r) => setTimeout(r, 300));
  }
  return {found: false, ms: Date.now() - t0, count: 0, url: location.href,
          title: document.title, ready: document.readyState};
})()"""


class BrowserAgent:
    """单身份单实例的浏览器会话（惰性启动）。"""

    def __init__(self, identity: str, site: str | None = None, *,
                 allow_local: bool = False) -> None:
        self.identity_name = identity
        self.site = site
        self.allow_local = allow_local
        self._l: Any = None
        self._guard: Guard | None = None
        self._profile: dict = {}
        self._refs: dict[int, tuple[Any, str]] = {}
        self._cursor: humanize.Cursor | None = None
        self._waiting_human: str = ""
        self._last_snapshot: str = ""
        home = config.home_dir()
        self.downloads_dir = home / "downloads" / identity

    # ---------- 生命周期 ----------

    async def ensure_started(self) -> None:
        if self._l is not None:
            return
        ident = load_identity(self.identity_name, site=self.site)
        self._profile = profile_for(ident.site)
        if ident.profile_mode == "user":
            # 共用用户资料目录模式：他的浏览器开着 = 资料目录被占着，**不硬抢**
            running, detail = user_browser_running(ident.channel)
            if running:
                raise GuardError(
                    f"你的日常浏览器正在运行（{detail}）——Chromium 同一个资料目录同时只能一个实例，"
                    f"所以我没法在它开着的时候接管你的登录态。\n"
                    f"两个办法（**先跟用户确认再动手**）：\n"
                    f"  ① 让用户自己关掉浏览器（**登录态不会丢**），关完调 browse_resume；\n"
                    f"  ② 用户同意后调 browse_takeover()，我帮他优雅关掉再继续。")
        self._l = await launch(ident, self._profile)
        self._guard = Guard(self._profile, ident, allow_local=self.allow_local)
        self._cursor = humanize.Cursor()
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._guard.audit.log("session_start", channel=ident.channel, site=ident.site)

    async def ensure_alive(self) -> None:
        """窗口被人手动关了？复位状态——下次调用会重新开窗，而不是抛 "Target closed"。

        （守护进程每次调用前会调它：人在窗口里操作完直接叉掉窗口是很正常的动作。）
        """
        if self._l is None:
            return
        try:
            closed = self._l.ctx.is_closed()
        except Exception:
            closed = True
        if closed:
            self._l = None
            self._guard = None

    async def close_if_local(self) -> None:
        """本进程要退了：**进程内模式**该关浏览器（留着也没人管得了）；
        带守护进程的模式下**什么都不做**——MCP 服务被宿主回收是常态，浏览器必须留在守护进程里。

        2026-09-24 实测踩到：`mcp_server` 的 finally 直接调了 `close()`，于是"宿主回收 MCP 服务"
        变成了"顺手把浏览器也关了"，守护进程白做。
        """
        await self.close()

    async def save_state(self) -> int:
        """把 cookie（**含会话级**）落盘——扫码登录能跨"窗口重开"活下来全靠它。"""
        if self._l is None:
            return 0
        try:
            return await session_mod.dump_cookies(self._l.ctx, self.identity_name)
        except Exception:
            return 0

    async def close(self) -> str:
        if self._l is not None:
            await self.save_state()          # 关之前先落盘，别把登录票据丢了
            if self._guard:
                self._guard.audit.log("session_end")
            await launch_close(self._l)   # 优雅关闭：profile（cookie 等）正常落盘
            self._l = None
            self._guard = None
        return "已优雅关闭浏览器（登录态已落盘）。"

    @property
    def is_open(self) -> bool:
        """浏览器窗口是否还开着（守护进程的空闲自动关闭据此判断）。"""
        return self._l is not None

    @property
    def page(self):
        return self._l.page

    def _loc(self, ref: int):
        item = self._refs.get(int(ref))
        if item is None:
            raise GuardError(f"编号 [{ref}] 不存在或已失效——页面可能已变化，请先 browse_snapshot 重新取号")
        return item[0], item[1]

    def _need_human(self) -> str | None:
        if self._waiting_human:
            return (f"已停手等人工：{self._waiting_human}\n"
                    f"人工在浏览器窗口里处理完 → 调 browse_resume 继续。")
        return None

    # ---------- 状态摘要（写动作必带） ----------

    async def _state(self) -> str:
        parts = [f"URL {self.page.url[:90]}"]
        try:
            t = (await self.page.title())[:50]
            if t:
                parts.append(f"标题 {t}")
        except Exception:
            pass
        n = await snap_mod.count_matches(self.page, self._profile.get("list_item", ""))
        if n >= 0:
            parts.append(f"列表 {n} 条")
        return " ｜ ".join(parts)

    async def _state_delta(self, before_url: str, before_count: int) -> str:
        parts = [f"URL {'未变' if self.page.url == before_url else self.page.url[:90]}"]
        n = await snap_mod.count_matches(self.page, self._profile.get("list_item", ""))
        if n >= 0 and before_count >= 0:
            parts.append(f"列表 {before_count} 条 → {n} 条")
        return " ｜ ".join(parts)

    # ---------- 工具面 ----------

    async def open(self, url: str) -> str:
        await self.ensure_started()
        assert self._guard is not None and self._l is not None
        human = self._need_human()
        if human:
            return human
        try:
            await self._guard.before_nav(url)
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError:
            # 加载中发生重定向会让驱动丢执行上下文（症状酷似"被风控"）——独立通道解析最终 URL 重进
            final = await resolve_final_url(url, channel=self._l.channel)
            if not final or final == url:
                raise
            await self._rebind_page()
            assert self._guard
            await self._guard.before_nav(final)
            await self.page.goto(final, wait_until="domcontentloaded", timeout=30_000)
        await humanize.delay("after_navigate")
        hit = await self._canary_hit("导航之后")
        if hit:
            return hit
        text = await self.snapshot()
        await self.save_state()      # 每次导航后顺手落盘：进程被强杀也不至于丢登录票据
        # 到达一个站点时把攒下的经验顶在前面（只在这里读，别塞进每次快照——省 token）
        notes = notes_mod.read(self.page.url)
        if notes:
            text = (f"—— 站点经验（可能过时，只作提示；与实测不符时按实测来，并更新它）——\n"
                    f"{notes}\n—— 站点经验结束 ——\n{text}")
        return text

    async def _rebind_page(self) -> None:
        assert self._l is not None
        old = self._l.page
        self._l.page = await self._l.ctx.new_page()
        try:
            await old.close()
        except Exception:
            pass

    async def snapshot(self) -> str:
        await self.ensure_started()
        assert self._guard
        self._guard.ensure_ready()
        text, refs = await snap_mod.snapshot(self.page)
        self._refs = refs
        self._last_snapshot = text
        return text

    async def resnapshot(self) -> str:
        """上一步没变化时重新取号（工具面用；语义与 snapshot 相同）。"""
        return await self.snapshot()

    async def _adopt_new_page(self, before: list) -> str:
        """站点把内容开在**新标签页**时，把"眼睛"跟过去（真人看到的就是新标签跳到前面）。

        为什么必须有：不少站点的卡片/详情是 `target=_blank`——不跟过去，`self.page` 还停在
        列表页，agent 会以为"点了没反应"，然后开始乱试（坑 #2 的原型）。
        返回一行说明；没换页就是空串。
        """
        if self._l is None:
            return ""
        fresh = [p for p in self._l.ctx.pages if p not in before]
        if not fresh:
            return ""
        page = fresh[-1]
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        try:
            await page.bring_to_front()
        except Exception:
            pass
        self._l.page = page
        return f"站点开了新标签页，已切过去（旧标签留着，共 {len(self._l.ctx.pages)} 个）"

    async def _locate(self, selector: str):
        """按 CSS 选择器定位（快照没编号时的兜底）。

        存在的理由（2026-09-24 真站点实测）：编号只覆盖"选择器能命中的元素"，
        动态渲染站点的可点元素形状千奇百怪；与其让 agent 绕页面，不如给它一条正当的路——
        **用 browse_extract 找到它（读），再用这里点它（写）**，写动作的护栏一样不少。
        """
        try:
            loc = self.page.locator(selector)
            n = await loc.count()
        except Exception as e:
            raise GuardError(f"CSS 选择器不合法（{selector}）：{str(e)[:120]}") from None
        if n == 0:
            raise GuardError(
                f"选择器没匹配到任何元素：{selector}\n"
                f"先用 browse_extract 确认它此刻真的存在（页面可能已变，或在 iframe 里）")
        kind = "选择器" if n == 1 else f"选择器(命中{n}个,点第1个)"
        return loc.first, kind

    async def _canary_hit(self, when: str) -> str | None:
        """站点档案声明的"判死信号"命中就立刻停手等人（不做没依据的规则）。

        只看当前 URL 是否命中 ``canary.bad_url_patterns``（about:blank / 验证码页等）——
        页面内的更多信号（"操作频繁"弹窗之类）交给提示词层，这里不硬编码。
        """
        pats = ((self._profile.get("canary") or {}).get("bad_url_patterns")) or []
        url = self.page.url
        for p in pats:
            if p and p in url:
                reason = f"当前页面命中站点档案里的异常/风控信号「{p}」：{url}"
                self._waiting_human = reason
                if self._guard:
                    self._guard.audit.log("canary_stop", pattern=p, url=url, when=when)
                return (f"⚠ 已停手（{when}）：{reason}\n"
                        f"按纪律**不要重试、不要想办法绕**。请人工在窗口里确认，"
                        f"处理完调 browse_resume 再继续。")
        return None

    async def click(self, ref: int | None = None, selector: str = "", path: str = "auto") -> str:
        """点击。``ref`` 用快照编号；快照里没有的元素用 ``selector``（CSS）兜底。

        两条路都走真实鼠标事件，护栏、审计、"写后状态摘要 + 新快照"完全一致——
        区别只是**从哪里找到这个元素**。
        """
        await self.ensure_started()
        assert self._guard
        human = self._need_human()
        if human:
            return human
        if selector:
            loc, kind = await self._locate(selector)
            where = f"选择器 {selector}"
        elif ref is not None:
            loc, kind = self._loc(ref)
            where = f"[{ref}]"
        else:
            raise GuardError("要点击，就得告诉我在点谁：给快照编号 ref，或给 CSS 选择器 selector")
        use_path = KIND_PATH.get(kind, "curve") if path == "auto" else path
        self._guard.before_action("click", ref=ref, selector=selector or None, kind=kind, path=use_path)
        before_url, before_count = self.page.url, await snap_mod.count_matches(
            self.page, self._profile.get("list_item", ""))
        before_pages = list(self._l.ctx.pages) if self._l is not None else []
        self._cursor = await humanize.click(self.page, loc, self._cursor, path=use_path)
        switched = await self._adopt_new_page(before_pages)      # target=_blank 的站点：跟过去
        state = await self._state_delta(before_url, before_count)
        if switched:
            state += f" ｜ {switched}"
        hit = await self._canary_hit("点击之后")
        if hit:
            return hit
        text, refs = await snap_mod.snapshot(self.page)
        self._refs = refs
        return f"已点击 {where}（{kind}）。\n状态：{state}\n{text[:3000]}"

    async def type_text(self, ref: int, text: str, submit: bool = False) -> str:
        await self.ensure_started()
        assert self._guard
        human = self._need_human()
        if human:
            return human
        loc, kind = self._loc(ref)
        self._guard.before_action("type", ref=ref, submit=submit, chars=len(text))
        self._cursor = await humanize.type_text(self.page, loc, text,
                                                submit=submit, cursor=self._cursor, path="direct")
        base = f"已在 [{ref}] 逐键输入 {len(text)} 个字符" + ("并回车提交。" if submit else "。")
        n = await snap_mod.count_interactive(self.page)     # 输入常触发联想/校验，给个可对比的量
        return base + (f"\n页面可交互元素：{n}（变了说明触发了联想或校验）" if n >= 0 else "")

    async def scroll(self, direction: str = "down", amount: int = 600) -> str:
        await self.ensure_started()
        assert self._guard
        human = self._need_human()
        if human:
            return human
        self._guard.before_action("scroll", direction=direction, amount=amount)
        dy = abs(int(amount)) * (-1 if direction == "up" else 1)
        before = await snap_mod.count_interactive(self.page)
        await humanize.scroll(self.page, dy)
        after = await snap_mod.count_interactive(self.page)
        extra = ""
        if before >= 0 and after >= 0:
            extra = f"\n页面可交互元素：{before} → {after}"
            if after == before:
                extra += "（没变——多半还没加载完，再滚一次或稍等一拍再取快照）"
        return f"已滚动 {direction} {abs(int(amount))}px。{extra}"

    async def press(self, key: str) -> str:
        await self.ensure_started()
        assert self._guard
        human = self._need_human()
        if human:
            return human
        self._guard.before_action("press", key=key)
        before_pages = list(self._l.ctx.pages) if self._l is not None else []
        await self.page.keyboard.press(key)
        switched = await self._adopt_new_page(before_pages)      # 回车打开新标签也跟过去
        await humanize.delay("after_interact")
        return f"已按键 {key}。"

    async def back(self) -> str:
        await self.ensure_started()
        assert self._guard
        human = self._need_human()
        if human:
            return human
        self._guard.before_action("back")
        await self.page.go_back(wait_until="domcontentloaded")
        await humanize.delay("after_navigate")
        return await self.snapshot()

    async def wait_for(self, until: str, timeout_sec: int = 15) -> str:
        """等页面上出现目标内容——**"导航返回"不等于"内容就绪"**（借鉴 web-access 的成熟经验）。

        `until` 以 ``css=`` 开头按 CSS 选择器判，否则按"可见文字包含"判。
        有界轮询，一次求值干完；**超时不抛错**，如实报告现状并给下一步建议——
        免得 agent 把"还没加载出来"误判成"内容不存在"。
        """
        await self.ensure_started()
        assert self._guard
        human = self._need_human()
        if human:
            return human
        needle, is_css = (until[4:], True) if until.startswith("css=") else (until, False)
        if not needle.strip():
            raise GuardError("browse_wait 要告诉我在等什么：一段可见文字，或 css=选择器")
        timeout_ms = max(1, min(int(timeout_sec), 60)) * 1000
        self._guard.before_action("wait", until=until, timeout_sec=timeout_sec)
        js = _WAIT_JS % {"needle": json.dumps(needle, ensure_ascii=False),
                         "is_css": "true" if is_css else "false",
                         "timeout": timeout_ms}
        data = await eval_isolated(self.page, js, timeout_ms=timeout_ms + 8000)
        waited = (data or {}).get("ms", 0) / 1000
        if (data or {}).get("found"):
            head = (f"✓ 目标已出现（等了 {waited:.1f}s，命中 {(data or {}).get('count', 1)} 处）：{until}")
        else:
            head = (f"⚠ 等了 {waited:.1f}s 没等到「{until}」——**别当成「内容不存在」**：页面可能还在加载、"
                    f"或在验证/登录跳转这类中间态。先看下面的快照判断，必要时再 browse_wait 一次。")
        text, refs = await snap_mod.snapshot(self.page)
        self._refs = refs
        return f"{head}\n{text[:3000]}"

    async def text(self, limit: int = 6000) -> str:
        await self.ensure_started()
        assert self._guard
        self._guard.ensure_ready()
        # 在页面里先截断再带回来：整页正文可能几百 KB，拉回来再切纯属白等
        try:
            body = await eval_isolated(
                self.page,
                f"((document.body && document.body.innerText) || '').slice(0, {int(limit) + 1})",
                timeout_ms=6000)
            return str(body or "")[:limit]
        except Exception:
            body = await self.page.inner_text("body", timeout=8000)   # JS 通道不通时退回 locator
            return body[:limit]

    async def takeover(self) -> str:
        """（共用用户资料目录模式）用户同意后，帮他把日常浏览器**优雅**关掉，让出资料目录。

        **调用前必须先跟用户确认**——那是他的浏览器（可能正开着别的标签页/表单）。
        只发关闭信号、不强制结束；关不掉就如实回报（多半是弹了"关闭所有标签页吗"）。
        """
        ident = load_identity(self.identity_name, site=self.site)
        if ident.profile_mode != "user":
            return "当前不是「共用用户资料目录」模式，不需要接管——直接调工具就行。"
        ok, detail = close_user_browser(ident.channel)
        if self._guard:
            self._guard.audit.log("takeover", ok=ok, detail=detail)
        if not ok:
            raise GuardError(f"没能关掉用户的浏览器：{detail}")
        return (f"已关闭用户的日常浏览器（{detail}）。现在可以正常干活了——"
                f"下一次工具调用会自动用他的登录态开窗。\n"
                f"记得提醒用户：**接下来这段时间他的浏览器用不了**；他要用时你收尾（browse_close）。")

    async def remember(self, fact: str) -> str:
        """把一条**验证过的**站点经验记下来（按域名存；下次到该站点会自动顶在快照开头）。"""
        await self.ensure_started()
        assert self._guard
        self._guard.ensure_ready()
        url = self.page.url
        try:
            path = notes_mod.append(url, fact)
        except ValueError as e:
            raise GuardError(str(e)) from None
        self._guard.audit.log("remember", url=url[:120], fact=str(fact)[:120])
        return (f"已记下（站点 {notes_mod.domain_of(url)}）：{fact}\n"
                f"文件：{path}\n"
                f"下次 browse_open 到这个站点会自动带上它。只记**亲眼验证过**的事实，别记猜测。")

    async def screenshot(self) -> str:
        await self.ensure_started()
        assert self._guard
        self._guard.ensure_ready()
        path = self.downloads_dir / f"shot_{int(time.time())}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=False)
        except Exception:
            await self.page.screenshot(path=str(path), full_page=False, animations="disabled")
        self._guard.audit.log("screenshot", file=str(path))
        return f"截图已保存：{path}"

    async def download(self, ref: int) -> str:
        await self.ensure_started()
        assert self._guard
        human = self._need_human()
        if human:
            return human
        loc, kind = self._loc(ref)
        self._guard.before_action("download", ref=ref)
        async with self.page.expect_download(timeout=60_000) as dl_info:
            self._cursor = await humanize.click(self.page, loc, self._cursor,
                                                path="direct", after=False)
        dl = await dl_info.value
        path = self.downloads_dir / (dl.suggested_filename or f"file_{int(time.time())}")
        await dl.save_as(str(path))
        return f"已下载：{path}"

    # ---------- 开放读取通道（只读；跑在隔离世界） ----------

    async def eval_read(self, expression: str, world: str = "isolated") -> str:
        await self.ensure_started()
        assert self._guard
        self._guard.ensure_ready()
        self._guard.audit.log("eval", world=world, chars=len(expression))
        value = (await eval_isolated(self.page, expression) if world == "isolated"
                 else await eval_main(self.page, expression))
        return render(value)

    async def extract(self, expression: str) -> str:
        """结构化读取：表达式须返回 JSON 字符串（用 JSON.stringify 包一层）。"""
        raw = await self.eval_read(expression, world="isolated")
        import json as _json
        try:
            data = _json.loads(raw)
        except Exception:
            raise GuardError("browse_extract 要求表达式返回 JSON 字符串（用 JSON.stringify(...) 包一层）；"
                             "如果只是随便看看，请用 browse_eval") from None
        self._guard_audit_extract(len(_json.dumps(data, ensure_ascii=False)))
        return render(data)

    def _guard_audit_extract(self, size: int) -> None:
        if self._guard:
            self._guard.audit.log("extract", size=size)

    # ---------- 人机交接 ----------

    async def login_status(self) -> str:
        await self.ensure_started()
        assert self._guard
        check = self._profile.get("login_check")
        if not check:
            return ("本站未配置登录判据（档案里没有 login_check）——无法自动判断。\n"
                    "做法：browse_open 打开目标页 → browse_snapshot 看是否被跳到登录页；"
                    "需要人工登录时调 browse_ask_human。")
        try:
            value = await eval_isolated(self.page, check)
        except Exception as e:
            return f"登录检查执行失败：{e}——请人工在窗口里确认。"
        state = "ok 看起来已登录" if value else "not_logged_in 未登录"
        return (f"登录态：{state}\n"
                f"  判据是站点档案里的 login_check（**弱判据**：只看有没有被弹到登录页、"
                f"页面上有没有登录入口）。\n"
                f"  最终以目标内容为准：拿得到要的东西就算登录了；拿不到、且判断是登录问题，"
                f"再 browse_ask_human 叫人。")

    async def ask_human(self, reason: str) -> str:
        await self.ensure_started()
        assert self._guard
        self._waiting_human = reason
        self._guard.audit.log("ask_human", reason=reason)
        return (f"已停手，等人工处理。\n原因：{reason}\n"
                f"请让人在**当前这个浏览器窗口**里完成（扫码/验证码/滑块/申诉），"
                f"处理完告诉我，我调 browse_resume 继续。")

    async def resume(self) -> str:
        await self.ensure_started()
        self._waiting_human = ""
        if self._guard:
            self._guard.audit.log("resume")
        return "已恢复。先看一眼当前页面再决定下一步：\n" + await self.snapshot()
