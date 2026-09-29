// @race 竞态矩阵：登录态与网络
// 场景 A：双端互踢——context1 登出 → context2 操作被 401+E006 引导回 /login
// 场景 B：断网恢复——对话中断网 → ⚠️ 气泡 → 恢复重发 → 正常
// 2026-08-13 单点登录适配：A 不再两端各自登录（第二次登录会踢第一个，登出端 token 已死
// 无法触发吊销）——改为 shareAuth 复制 cookie 共享同一登录态。
import { test, expect } from '@playwright/test'
import { loginViaUI, shareAuth } from '../helpers/auth'
import { waitStreamEnd, bubbleRoles } from '../helpers/wait'
import { SHORT_QUESTION } from '../helpers/constants'

test.setTimeout(180_000)

test('@race A 双端互踢：一端登出另一端被踢回登录页', async ({ browser }) => {
  // 两个独立上下文（同一账号 chaos01，共享同一登录态）
  const ctx1 = await browser.newContext()
  const ctx2 = await browser.newContext()
  const page1 = await ctx1.newPage()
  const page2 = await ctx2.newPage()
  await loginViaUI(page1)
  await shareAuth(page1, ctx2)
  await page2.goto('/qa')
  await page2.locator('.chat-input').waitFor()
  console.log('[raceA] 两端共享登录态就绪')

  // context1 登出（吊销 token_version）
  await page1.locator('.sidebar-item', { hasText: '退出登录' }).click()
  await page1.waitForURL('**/login', { timeout: 15_000 })
  console.log('[raceA] context1 已登出')

  // 预清 context2 本地 user（同 fixB6：防登录页读残留 user 自动跳回造成落点竞态）
  await page2.evaluate(() => localStorage.removeItem('user'))
  // context2 触发 API（整页导航 → 挂载请求 401+E006 → 拦截器跳 /login）
  await page2.goto('/home')
  await expect(page2).toHaveURL(/\/login/, { timeout: 20_000 })
  console.log('[raceA] context2 被踢回登录页（双端互踢生效）')

  await ctx1.close()
  await ctx2.close()
})

test('@race B 断网恢复：⚠️ 气泡后重发正常', async ({ page }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 断网 → 发送 → 应出现错误气泡（E016）
  await page.context().setOffline(true)
  await page.locator('.chat-input').fill(SHORT_QUESTION)
  await page.keyboard.press('Enter')
  // 2026-09-18：改用**文案**判定，不再靠 '⚠️' 前缀——qaStore 去 emoji 后错误气泡不带前缀了
  // （全站图标统一 Phosphor）。断网路径的文案是「网络中断，已停止接收」或「请求失败」。
  await expect
    .poll(
      async () =>
        (await bubbleRoles(page)).some(
          (b) => b.text.includes('网络中断') || b.text.includes('请求失败')),
      // 2026-09-18：窗口 20s → 45s。sse.ts 断网会先指数退避重试 RETRY_DELAYS=[1,2,4,8,16]s
      // （合计 31s）才 onError，20s 窗口必然先超时——原失败被误当成"没有错误提示"
      { timeout: 45_000 },
    )
    .toBe(true)
  console.log('[raceB] 断网发送出现错误气泡')

  // 恢复网络 → 重发 → 正常完成
  await page.context().setOffline(false)
  await page.locator('.chat-input').fill('恢复网络后的消息')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  const bubbles = await bubbleRoles(page)
  const users = bubbles.filter((b) => b.role === 'user').length
  console.log(`[raceB] 恢复重发后 user 气泡=${users}`)
  expect(users, '[raceB] 恢复后应能正常发送并看到消息').toBeGreaterThanOrEqual(1)
})
