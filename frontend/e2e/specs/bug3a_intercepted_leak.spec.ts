// @bug3a 用户报告：破甲（越狱）对话被拦截后，发送下一条对话时，上一条被拦截的消息仍会被 agent 看到。
//
// 机制：E010 拦截轮在 chat_service :610-616 走「不发 done，降级落库」——被拦 user 消息落库；
// 下一轮 _load_history :488-498 注入历史 → 被拦截文本进入 LLM 上下文。
// 复现：发「破甲测试标记 M5X9K」触发 E010（input_filter 单点危险词 100% 命中）→
//       发「请复述我刚才上一条消息的原文」→ 回复含 M5X9K = 泄漏确认。
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient, apiSessionMessages, userTexts } from '../helpers/api'
import { waitStreamEnd, bubbleRoles } from '../helpers/wait'
import { collectErrors } from '../helpers/console'
import { SHORT_QUESTION, JAILBREAK_QUESTION, JAILBREAK_MARK } from '../helpers/constants'

test.setTimeout(240_000)

test('@real-llm @bug3a 破甲拦截后下一条消息仍被 agent 看到（泄漏核实）', async ({ page, request }) => {
  await loginViaUI(page)
  const errs = collectErrors(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 1. 建会话（正常消息）
  await page.locator('.chat-input').fill(SHORT_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  const sid = (await page.evaluate(() => localStorage.getItem('qa_current_session'))) as string

  // 2. 发送破甲问题（100% 触发 E010 拦截）
  await page.locator('.chat-input').fill(JAILBREAK_QUESTION)
  await page.keyboard.press('Enter')
  // 等拦截结果气泡（⚠️ 开头 = onError 渲染）
  await expect
    .poll(
      async () => {
        const bubbles = await bubbleRoles(page)
        const last = bubbles[bubbles.length - 1]
        return last ? last.text.startsWith('⚠️') : false
      },
      { timeout: 30_000 },
    )
    .toBe(true)
  const intercepted = await bubbleRoles(page)
  const interceptText = intercepted[intercepted.length - 1].text
  console.log(`[bug3a] 拦截气泡: ${interceptText.slice(0, 80)}`)

  // 3. 事实记录：被拦 user 行是否落库（按设计 E010 短路会降级落库）
  const api = apiClient(page)
  const m1 = await apiSessionMessages(api, sid)
  const u1 = m1.ok ? userTexts(m1.data.messages) : []
  const leakedRow = u1.some((t) => t.includes(JAILBREAK_MARK))
  console.log(`[bug3a] 被拦 user 行已落库=${leakedRow}（user 行数=${u1.length}）`)

  // 4. 发下一条正常问题，让 agent 复述上一条
  await page.locator('.chat-input').fill('请复述我刚才上一条消息的原文')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  const bubbles = await bubbleRoles(page)
  const lastAssistant = [...bubbles].reverse().find((b) => b.role === 'assistant')
  const reply = lastAssistant ? lastAssistant.text : ''
  console.log(`[bug3a] 复述回复: ${reply.slice(0, 150)}`)

  const errText = errs.describe()
  if (errText) console.log(`[bug3a] console/pageerror: ${errText}`)

  // 5. 判定：泄漏 = bug 确认（FAIL）；未泄漏 = PASS 记录
  const leaked = reply.includes(JAILBREAK_MARK)
  if (leaked) {
    console.log(
      `[bug3a] ROOTCAUSE_CONFIRMED: 被拦截消息「${JAILBREAK_QUESTION}」经 _load_history 泄漏至下一轮上下文，agent 回复含标记 ${JAILBREAK_MARK}`,
    )
  } else {
    console.log('[bug3a] 未复现泄漏（WARNING：本 LLM 回复未含标记，不代表上下文未注入）')
  }
  expect(leaked, `[bug3a] BUG确认: 破甲拦截后下一条消息泄漏（回复含标记 ${JAILBREAK_MARK}）`).toBe(false)

  // 清理
  if (sid) await api.delete(`/api/v1/chat/sessions/${sid}`).catch(() => {})
})
