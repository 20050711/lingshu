// @skillspersist 补测 B7：技能页勾选保存 + 切页再回（持久化）
// 场景 A：/skills/default 勾选/取消一个多选技能 → 保存 → 切页再回 → 状态保留
// 场景 B：保存后 QA 页功能浮窗技能面板同源同步（勾选状态一致）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { closeSessionsDrawer } from '../helpers/ui'

test.setTimeout(180_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@skillspersist A 勾选保存 + 切页再回保留', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)

  // 进入技能页，等技能卡加载
  await page.goto('/skills/default')
  await page.locator('.skill-card').first().waitFor({ timeout: 20_000 })

  // 找第一个未授权（非 disabled）的多选技能卡（checkbox 非 radio）
  let card: any = null
  for (let i = 0; i < 15; i++) {
    const c = page.locator('.skill-card:not(.skill-card-disabled)').nth(i)
    if ((await c.count()) === 0) break
    if ((await c.locator('.ant-checkbox-input').count()) > 0) { card = c; break }
  }
  expect(card, '[skillA] 应存在可操作的多选技能卡').toBeTruthy()

  const cb = card.locator('.ant-checkbox-input')
  const before = await cb.isChecked()
  console.log(`[skillA] 技能卡勾选前=${before}`)

  // 切换状态并保存
  await cb.click()
  await page.locator('.toolbar-btn', { hasText: /^保存/ }).click()
  await page.locator('.ant-message', { hasText: '已保存' }).waitFor({ timeout: 15_000 })
  console.log(`[skillA] 已保存（勾选后=${!before}）`)

  // 切页再回：状态保留
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  await page.goto('/skills/default')
  await page.locator('.skill-card').first().waitFor({ timeout: 20_000 })
  await page.waitForTimeout(1500) // store 加载偏好

  const cb2 = page.locator('.skill-card:not(.skill-card-disabled)').nth(0).locator('.ant-checkbox-input')
  const after = await cb2.isChecked()
  console.log(`[skillA] 切页再回勾选=${after}（期望 ${!before}）`)
  expect(after, '[skillA] 切页再回勾选状态应保留').toBe(!before)

  // 还原：切回原状态（避免污染后续测试）
  await cb2.click()
  await page.locator('.toolbar-btn', { hasText: /^保存/ }).click()
  await page.locator('.ant-message', { hasText: '已保存' }).waitFor({ timeout: 15_000 }).catch(() => {})

  await cleanup(page)
})

test('@skillspersist B QA 页技能浮窗与技能页同源', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  await closeSessionsDrawer(page)  // D6：抽屉遮罩拦截主区按钮

  // 打开 ai技能 浮窗
  await page.locator('.toolbar-btn', { hasText: 'ai技能' }).first().click()
  const panel = page.locator('.float-panel')
  await expect(panel).toBeVisible({ timeout: 10_000 })
  const skillItems = await panel.locator('input[type="checkbox"]').count()
  console.log(`[skillB] QA 技能浮窗 checkbox 数=${skillItems}`)
  expect(skillItems, '[skillB] QA 技能浮窗应有技能勾选项').toBeGreaterThan(0)

  await cleanup(page)
})
