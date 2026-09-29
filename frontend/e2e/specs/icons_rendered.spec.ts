// 图标渲染回归（2026-09-22）：智能助手工具栏那两个浮窗（ai技能 / 外部工具）**必须画图标，不能把
// 后端的语义名当文本打出来**（曾整排显示 "book 语义名" 这类字面量）。
//
// 背景：后端 icon 字段 2026-09-18 起是**语义名**（book/link/database…，白名单见
// backend/app/agent/tools/__init__.py 的 ICON_KEYS），画成什么图标由前端 components/Icon.tsx 决定。
// 有几处地方漏改、直接 `{t.icon} {t.name}` 渲染 → 界面上就是一堆英文单词；另有 MCP 工具行的 icon
// 被 migrate_xhs_merge.py 重跑时写回了 emoji 📕。
//
// 断言口径：浮窗里每一条 (.skill-item) 都要有 svg.ph-icon，且文本不能以"图标键+空格"开头。
// 账号：chaos01（乱操部，helpers/auth.loginViaUI）。
import { expect, test } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'

// 与后端 ICON_KEYS 同源（漏了新键也不该漏判：再兜 emoji）
const ICON_KEY = /^(tool|chart|video|audio|image|film|screen|code|brain|file|note|book|book-open|folder|database|package|compress|search|link|download|globe|mail|calendar|building|chat|check|question|refresh|palette)\s/
const EMOJI = /^[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]\s/u

async function openPanelAndCheck(page: import('@playwright/test').Page, button: string, shot: string) {
  await page.getByRole('button', { name: button, exact: true }).click()
  const panel = page.locator('.float-panel').first()
  await panel.waitFor({ state: 'visible' })
  const items = panel.locator('.skill-item')
  await expect(items.first()).toBeVisible()
  const n = await items.count()
  expect(n, `${button} 浮窗应有条目`).toBeGreaterThan(0)

  const badText: string[] = []
  const noIcon: string[] = []
  for (let i = 0; i < n; i++) {
    const it = items.nth(i)
    const text = (await it.innerText()).replace(/\s+/g, ' ').trim()
    if (ICON_KEY.test(text) || EMOJI.test(text)) badText.push(text)
    if ((await it.locator('svg.ph-icon').count()) === 0) noIcon.push(text)
  }
  await page.screenshot({ path: `/tmp/e2e_icon_${shot}.png`, clip: await panel.boundingBox() ?? undefined })
  expect(badText, `${button}：这些条目把图标名当文本渲染了 → ${JSON.stringify(badText)}`).toEqual([])
  expect(noIcon, `${button}：这些条目没有渲染图标 svg → ${JSON.stringify(noIcon)}`).toEqual([])
}

test('@icons 智能助手两个浮窗：图标是画出来的，不是文本', async ({ page }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.toolbar-btn').first().waitFor()
  await openPanelAndCheck(page, 'ai技能', 'skills')
  await page.keyboard.press('Escape')
  await openPanelAndCheck(page, '外部工具', 'mcp')
})
