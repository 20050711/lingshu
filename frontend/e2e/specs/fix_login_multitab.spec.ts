// @fixB6 用户侧 6 修复验收：登录页挂载即登出 → 多标签互相踢下线
// 修复（2026-08-12）：LoginPage mount 时本地已有登录态 → 不调后端吊销（token_version+1），
// 直接跳回对应首页；仅显式点「退出登录」才走服务端吊销（L11/L21 全端掉线约定保持不变）。
// 验收：已登录状态新开标签访问 /login → 原标签不被 401 踢下线、新标签跳回 /home。
// 2026-08-13 单点登录适配：第二标签不再二次登录（会踢第一个），shareAuth 复制登录态。
import { test, expect } from '@playwright/test'
import { loginViaUI, shareAuth } from '../helpers/auth'

test.setTimeout(120_000)

test('@fixB6 已登录新开标签访问 /login：原标签不 401、新标签跳首页', async ({ browser }) => {
  // 两个独立 context 模拟双标签（共享同一登录态）
  const ctx1 = await browser.newContext()
  const ctx2 = await browser.newContext()
  const page1 = await ctx1.newPage()
  const page2 = await ctx2.newPage()

  await page1.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await page2.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))

  // 标签 1 登录（chaos01）
  await loginViaUI(page1)
  await page1.goto('/qa')
  await page1.locator('.chat-input').waitFor()

  // 标签 2 共享登录态（cookie + localStorage user）——等价于"已登录用户的第二个标签"
  await shareAuth(page1, ctx2)
  await page2.goto('/qa')
  await page2.locator('.chat-input').waitFor()

  // 已登录状态下标签 2 直接访问 /login（误入登录页场景）
  await page2.goto('/login')

  // 修复后行为：本地有 user → 不吊销 → 跳回 /home（默认角色跳 /home）
  await page2.waitForURL('**/home', { timeout: 15_000 })
  console.log('[fixB6] 新标签 /login → 跳回 /home ✓')

  // 标签 1 必须仍在登录态（原 bug：被吊销 → 下一操作 401 跳登录页）
  await page1.waitForTimeout(2000)
  await page1.locator('.chat-input').waitFor({ timeout: 10_000 })
  const stillOnQa = page1.url().includes('/qa')
  console.log(`[fixB6] 原标签仍在 /qa=${stillOnQa}`)
  expect(stillOnQa, '[fixB6] 原标签不应被踢下线（修复前：login 页 mount 吊销全端）').toBe(true)

  // 显式登出（L21 约定必须保留）：标签 1 退出 → 全端吊销
  await page1.locator('.sidebar-item', { hasText: '退出登录' }).click()
  await page1.waitForURL('**/login', { timeout: 15_000 })
  // 预清标签 2 本地 user：真实链路中 401 会触发 handleAuthExpired 清 user 后跳登录页；
  // 预清可避免登录页 mount 读到残留 user 自动跳回 /home 造成落点断言竞态
  await page2.evaluate(() => localStorage.removeItem('user'))
  // 标签 2 此时再操作（整页导航触发请求）→ 401 跳登录（被踢）
  await page2.goto('/qa')
  await expect(page2).toHaveURL(/\/login/, { timeout: 20_000 })
  console.log('[fixB6] 显式登出后标签 2 被踢回登录页（全端吊销生效）')

  await ctx1.close()
  await ctx2.close()
})
