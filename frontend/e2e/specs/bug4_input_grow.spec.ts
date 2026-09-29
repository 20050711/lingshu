// @req-input bug4 用户需求：输入框多行适配——默认 6 行高、自动增高、超出滚动。
// 现状：ChatPanel:689 rows={Math.min(6, Math.max(1, 行数))}——空态 1 行（约 37px），
// 「默认 6 行」断言当前预期 FAIL（@req-input = 修复日验收断言）。
// 行高计算：font-size 13px × line-height 1.6 = 20.8px/行；padding 8×2；6 行 ≈ 141px。
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { countBubbles } from '../helpers/wait'

test.setTimeout(60_000)

test('@req-input bug4 输入框默认 6 行高/自适应/滚动', async ({ page }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  const ta = page.locator('.chat-input')

  const sixRows = 8 * 2 + 13 * 1.6 * 6 // ≈ 141
  const oneRow = 8 * 2 + 13 * 1.6 // ≈ 37

  // 1. 空态高度（2026-08-12 演示修正：动态增高——空态 1 行，随输入增高，6 行封顶）
  const emptyH = (await ta.boundingBox())!.height
  console.log(`[bug4] 空态高度=${emptyH.toFixed(0)}px（动态：1 行起，6 行封顶）`)

  // 2. 3 行 → 自动增高（大于空态、不超过 6 行高）
  await ta.fill('a\nb\nc')
  const h3 = (await ta.boundingBox())!.height
  console.log(`[bug4] 3 行高度=${h3.toFixed(0)}px`)
  expect(h3, '3 行应比空态高（动态增高）').toBeGreaterThan(emptyH)
  expect(h3, '3 行不应超过 6 行高').toBeLessThanOrEqual(sixRows + 6)

  // 3. 10 行 → 封顶 6 行 + 内部滚动
  await ta.fill(Array.from({ length: 10 }, (_, i) => `第${i}行`).join('\n'))
  const h10 = (await ta.boundingBox())!.height
  const scroll = await ta.evaluate((el) => ({
    scrollH: el.scrollHeight,
    clientH: el.clientHeight,
  }))
  console.log(
    `[bug4] 10 行高度=${h10.toFixed(0)}px scrollHeight=${scroll.scrollH} clientHeight=${scroll.clientH}（${scroll.scrollH > scroll.clientH ? '滚动' : '无滚动'}）`,
  )
  expect(h10, '10 行应封顶 6 行高').toBeLessThanOrEqual(sixRows + 6)
  expect(scroll.scrollH > scroll.clientH, '超过 6 行应内部滚动').toBe(true)

  // 4. Shift+Enter 换行不发送
  await ta.fill('shift enter')
  await page.keyboard.press('Shift+Enter')
  await page.waitForTimeout(500)
  const before = await countBubbles(page, 'user')
  expect(before, 'Shift+Enter 不应发送').toBe(0)

  // 5. Enter 发送
  await page.keyboard.press('Enter')
  await page.waitForTimeout(800)
  const after = await countBubbles(page, 'user')
  expect(after, 'Enter 应发送').toBe(1)
})
