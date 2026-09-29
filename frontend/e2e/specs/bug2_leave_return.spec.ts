// @bug2 用户报告：消息发出后切走页面，再切回问答界面，切换会话再切回 → 会话内容为空，
// 无法新建会话；向 agent 发送消息并等回复完成后，才能新建会话。
//
// 复现序列：发送 → 切「首页」→ 切回 /qa → 切到其他会话再切回原会话 → 观察内容/新建按钮
// 根因核实：API 对比 user 行数 + 新建按钮 disabled 状态变化
//   ① DB 有行 + 前端空 = 前端恢复/重建逻辑问题
//   ② DB 无行 = persist 未完成
//   ③ 新建长期 disabled = qaStore.streaming 卡 true（旧 SSE promise 未 resolve）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient, apiSessionMessages } from '../helpers/api'
import { waitStreamingStart, waitStreamEnd, countBubbles } from '../helpers/wait'
import { collectErrors } from '../helpers/console'
import { openSessionsDrawer } from '../helpers/ui'
import { LONG_QUESTION, SHORT_QUESTION } from '../helpers/constants'

test.setTimeout(240_000)

test('@bug2 发送后切页再回 + 切会话再切回 → 内容空/无法新建', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  const errs = collectErrors(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 1. 发送长问题
  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)

  // 2. 流式中切走页面（首页）再切回 QA
  await page.locator('.sidebar-item', { hasText: '首页' }).click()
  // 2026-08-12 首页新增第一行置灰卡（规划中）——.func-grid 现为 2 个，取第一个即可（仅作加载完成信号）
  await page.locator('.func-grid').first().waitFor({ timeout: 15_000 })
  await page.locator('.sidebar-item', { hasText: '智能助手' }).click()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(1000)
  const afterReturn = await countBubbles(page, 'user')
  const sid1 = (await page.evaluate(() => localStorage.getItem('qa_current_session'))) as string
  console.log(`[bug2] 切回 QA 后 user 气泡=${afterReturn}（sid=${sid1?.slice(0, 8)}）`)

  // 3. 会话切换再切回（若会话数 ≥2；只有 1 个会话则记录跳过）
  const items = page.locator('.session-item')
  const n = await items.count()
  let switched = false
  if (n >= 2) {
    // 切到另一会话（非 active 项）
    await items.locator(':not(.active)').first().click()
    await page.waitForTimeout(800)
    // 切回原会话（当前 active 之外的那个 = sid1）
    // 2026-09-18：上一步点击已**顺带收起抽屉**（会话项 onClick = switchSession + toggleSessions），
    // 不重开的话 .session-item 停在屏外、被左侧栏拦截（此前 chaos01 只剩 1 个会话、n>=2 进不去，
    // 缺陷被遮住；遗留会话一多就暴露）
    await openSessionsDrawer(page)
    await page.locator('.session-item:not(.active)').first().click()
    await page.waitForTimeout(1200)
    switched = true
  }
  const afterSwitchBack = await countBubbles(page, 'user')
  console.log(`[bug2] 切会话再切回后 user 气泡=${afterSwitchBack}（会话数=${n}，执行了切换=${switched}）`)

  // 4. 新建按钮状态（bug2 核心：无法新建）
  const newBtn = page.locator('.toolbar-btn', { hasText: '+ 新建' })
  const disabledNow = await newBtn.isDisabled().catch(() => false)
  console.log(`[bug2] 新建按钮 disabled=${disabledNow}（streaming 或 creatingNew 卡住时禁用）`)

  // 5. 等流结束，再断言新建按钮
  await waitStreamEnd(page)
  const disabledAfterStream = await newBtn.isDisabled().catch(() => false)
  console.log(`[bug2] 流结束后新建按钮 disabled=${disabledAfterStream}`)

  // 6. 发送一条，完成后断言新建可用
  await page.locator('.chat-input').fill(SHORT_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  const disabledAfterSend = await newBtn.isDisabled().catch(() => false)

  // 7. 后端核实：user 行数
  const api = apiClient(page)
  const msgs = await apiSessionMessages(api, sid1)
  const dbUserCount = msgs.ok
    ? (msgs.data.messages || []).filter((m: any) => m.role === 'user').length
    : -1
  console.log(`[bug2] DB user 行=${dbUserCount}`)
  const errText = errs.describe()
  if (errText) console.log(`[bug2] console/pageerror: ${errText}`)

  // 判定
  expect(dbUserCount, '[bug2] DB user 行为 0——消息从未落库').toBeGreaterThan(0)
  // 用户报告核心：「内容为空」+「无法新建」。未复现=消息保留且新建按钮最终可用。
  // 若复现（内容空），此处失败即 bug 确认态；bug2 是 bug1 的变体，判定借 bug1 根因
  if (afterSwitchBack === 0 && dbUserCount > 0) {
    console.log('[bug2] ROOTCAUSE_CONFIRMED: 切回后内容空但 DB 有行——前端重建/恢复逻辑问题（参考 bug1）')
  }
  expect(
    afterSwitchBack,
    `[bug2] BUG确认: 切页切回+切会话再切回后内容为空（DB有行=${dbUserCount > 0}）`,
  ).toBeGreaterThan(0)
  expect(disabledAfterSend, '[bug2] 发送完成后新建按钮仍禁用').toBe(false)

  // 清理
  if (sid1) await api.delete(`/api/v1/chat/sessions/${sid1}`).catch(() => {})
})
