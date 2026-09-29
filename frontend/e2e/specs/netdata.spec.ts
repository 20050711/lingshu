// @netdata 盲区 A：网络异常全景
// A1 断网后刷新：落库成功的消息能否恢复显示（数据可见性）
// A2 断网期间发送：⚠️ 提示必须出现（BUG-5 确认态——预期失败=确认 bug 仍在）
// A3 断网时点停止/切会话/新建：无卡死、UI 响应
// 注：A4（后端崩溃/重启）放手动脚本（kill 后端会中断 spec 运行）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { waitStreamEnd } from '../helpers/wait'
import { closeSessionsDrawer } from '../helpers/ui'
import { LONG_QUESTION, SHORT_QUESTION } from '../helpers/constants'

test.setTimeout(240_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@netdata A1 断网后刷新恢复：落库消息可恢复显示', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 发消息并等流完整结束（落库成功的前提）
  const MARK = '断网刷新可见性测试'
  await page.locator('.chat-input').fill(MARK)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  // 模拟断网（API 全断）：reload → 页面应显示错误/空（JS 从缓存加载，API 全失败）
  await page.route('**/api/v1/**', (route) => route.abort())
  await page.reload()
  await page.waitForTimeout(1500)
  const offlineState = await page.evaluate(() => document.body.innerText.length)
  console.log(`[netA1] 断网 reload: body 长度=${offlineState}`)

  // 恢复网络 → reload → 消息必须恢复（验证落库恢复）
  await page.unroute('**/api/v1/**')
  await page.reload()
  await page.waitForTimeout(3000)
  const text = await page.locator('body').innerText()
  const msgKept = text.includes(MARK)
  console.log(`[netA1] 恢复后 reload: 消息保留=${msgKept}`)
  expect(msgKept, '[netA1] 断网重连后落库消息应恢复显示').toBe(true)

  await cleanup(page)
})

test('@netdata A2 断网期间发送：必须有失败反馈且不卡死', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 2026-09-18 复核（模拟档实测）：原「BUG-5 确认态」标题的两条描述**均不成立**：
  // ① 本场景是**全新页面、尚无会话**——断网发送时先要 POST /chat/sessions 建会话，那一步就被
  //    abort，走 ChatPanel 的 catch → **toast 提示**（此刻没有会话可插入气泡，气泡无处可落）。
  //    原断言只找 .msg-bubble → 把"用 toast 反馈"误判成"完全无提示"。
  //    （对照：**已有会话**时走 SSE 路径，退避 RETRY_DELAYS=[1,2,4,8,16]s 合计 31s 后 onError
  //     弹气泡——该路径由 @race B 覆盖，已绿。）
  // ② 实测 `输入可用=true`、主线程正常 → "streaming 永久 true" 不成立。
  // 断言改为真正的用户价值：**有反馈**（气泡或 toast 任一）+ **不卡死**。
  // 发送前挂观察者：ant-message 约 3s 自动消失，事后 isVisible 必然扑空 → 记录"曾经出现过"。
  await page.evaluate(() => {
    (window as any).__sawToast = false
    new MutationObserver(() => {
      if (document.querySelector('.ant-message')) (window as any).__sawToast = true
    }).observe(document.body, { childList: true, subtree: true })
  })

  // 断网后发送
  await page.route('**/api/v1/**', (route) => route.abort())
  await page.locator('.chat-input').fill(SHORT_QUESTION)
  await page.keyboard.press('Enter')

  // 轮询"气泡或 toast"取先到者：新会话路径 toast 秒级、有会话路径气泡 31s 后退避完才出，
  // 45s 覆盖两者（固定 isVisible(45s) 会在 toast 路径白等满 45s）
  await expect
    .poll(async () => {
      const bubble = await page
        .locator('.msg-bubble', { hasText: '网络中断' })
        .or(page.locator('.msg-bubble', { hasText: '请求失败' }))
        .first().isVisible().catch(() => false)
      const toast = await page.evaluate(() => (window as any).__sawToast === true).catch(() => false)
      return bubble || toast
    }, { timeout: 45_000, message: '[netA2] 断网发送应给出失败反馈（气泡或 toast）' })
    .toBe(true)

  const inputOk = await page.locator('.chat-input').isEnabled().catch(() => false)
  const alive = await page.evaluate(() => Date.now()).then(() => true).catch(() => false)
  console.log(`[netA2] 断网发送: 输入可用=${inputOk} 主线程=${alive}`)
  expect(inputOk, '[netA2] 断网发送后输入框应恢复可用（不得永久 streaming）').toBe(true)
  expect(alive, '[netA2] 断网发送不应卡死主线程').toBe(true)

  // 恢复网络 → 重发正常（sse onError 应已复位 streaming）
  await page.unroute('**/api/v1/**')
  await page.locator('.chat-input').fill('恢复后重发')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  console.log('[netA2] 恢复后重发完成')

  await cleanup(page)
})

test('@netdata A3 断网时点停止/切会话/新建：无卡死', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 流式中断网 → 点停止 → 点新建 → 切会话
  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await page.waitForTimeout(800)
  await page.route('**/api/v1/**', (route) => route.abort())

  // D6（2026-08-14）：先关会话抽屉遮罩（遮罩拦截主区按钮）；会话操作再重开抽屉
  await closeSessionsDrawer(page)

  // 点停止（按钮在断网下也应响应——本地 abort，不发请求）
  const stop = page.locator('.btn-ghost', { hasText: '停止' })
  if (await stop.isVisible().catch(() => false)) await stop.click()
  await page.waitForTimeout(500)

  // 重开抽屉 → 点新建（本地状态操作）
  await page.locator('.toolbar-btn', { hasText: '会话列表' }).click().catch(() => {})
  await page.waitForTimeout(300)
  await page.locator('.toolbar-btn', { hasText: '+ 新建' }).click().catch(() => {})
  await page.waitForTimeout(500)

  // 切会话（会发请求失败——但不应卡死）
  const item = page.locator('.session-item:not(.active)').first()
  if (await item.count()) await item.click().catch(() => {})
  await page.waitForTimeout(1000)

  const alive = await page.evaluate(() => Date.now()).then(() => true).catch(() => false)
  const inputOk = await page.locator('.chat-input').isEnabled().catch(() => false)
  console.log(`[netA3] 断网乱操作: 主线程=${alive} 输入可用=${inputOk}`)
  expect(alive, '[netA3] 断网乱操作不应卡死').toBe(true)

  // 恢复
  await page.unroute('**/api/v1/**')
  await page.reload()
  await page.waitForTimeout(2000)

  await cleanup(page)
})
