// 逐个检查 ds 会话浏览区所有 tab 的图表渲染
import { test } from '@playwright/test'

test.setTimeout(180_000)

test('ds 浏览区全部 tab 检查', async ({ page }) => {
  await page.addInitScript(() => localStorage.clear())
  await page.goto('http://localhost:24426/login')
  await page.waitForTimeout(800)
  const resp = await page.request.post('http://localhost:24426/api/v1/auth/login', {
    data: { department_id: 'dianshang', username: 'ds', password: process.env.E2E_DS_PASSWORD ?? '' },
    headers: { 'Content-Type': 'application/json' },
  })
  await page.goto('http://localhost:24426/qa')
  await page.waitForSelector('textarea.chat-input', { timeout: 15000 })
  await page.locator('button.toolbar-btn', { hasText: '会话列表' }).click()
  await page.waitForTimeout(1500)
  const items = page.locator('.session-item')
  const n = await items.count()
  for (let i = 0; i < n; i++) {
    const txt = (await items.nth(i).textContent()) || ''
    if (txt.includes('看一下我们部门')) { await items.nth(i).click(); break }
  }
  await page.waitForTimeout(4000)
  const browseBtn = page.locator('button.toolbar-btn', { hasText: '浏览区' })
  if (await browseBtn.isVisible().catch(() => false)) await browseBtn.click()
  await page.waitForTimeout(2000)
  const tabs = page.locator('.output-tab')
  const tn = await tabs.count()
  console.log(`[ds] tab 总数=${tn}`)
  for (let i = 0; i < tn; i++) {
    const txt = (await tabs.nth(i).textContent()) || ''
    await tabs.nth(i).click()
    await page.waitForTimeout(2500)
    const f = await page.locator('text=function(v)').count()
    const u = await page.locator('text=/\\\\u[0-9a-fA-F]{4}/').count()
    const canvas = await page.locator('canvas').count()
    await page.screenshot({ path: `/tmp/ds_tab_${i}.png` })
    console.log(`[ds] tab${i}「${txt.slice(0, 24)}」 function(v)=${f} unicode转义=${u} canvas=${canvas}`)
  }
})
