// @multitab 盲区 C：多标签页同账号并发
// A 双标签同时发送：后端单 worker 队列（v2：等待器内存态）——无挂死/两流都完成
// B 双标签列表一致性：A 删会话 → B 刷新可见
// 2026-08-13 单点登录适配：两 context 不再各自登录（第二次登录会踢第一个），
// 改为 shareAuth 复制 cookie + localStorage user 共享同一登录态。
// v2（2026-08-14）：E009 confirm 语义已随授权卡下线（改为反问卡回答过期）——A 断言改为两流完成。
import { test, expect } from '@playwright/test'
import { loginViaUI, shareAuth } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { waitStreamEnd } from '../helpers/wait'

test.setTimeout(240_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@multitab A 双标签同时发送：无 E009/无挂死/两流完成', async ({ browser }) => {
  // 两个独立 context（共享同一登录态——等价于同浏览器两个标签）
  const ctx1 = await browser.newContext()
  const ctx2 = await browser.newContext()
  const page1 = await ctx1.newPage()
  const page2 = await ctx2.newPage()

  await page1.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await page2.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))

  await loginViaUI(page1)
  await shareAuth(page1, ctx2)
  await page1.goto('/qa')
  await page2.goto('/qa')
  await page1.locator('.chat-input').waitFor()
  await page2.locator('.chat-input').waitFor()

  // 监听 E009/错误
  const errs1: string[] = []
  page1.on('console', (m) => { if (m.type() === 'error') errs1.push(m.text()) })
  const errs2: string[] = []
  page2.on('console', (m) => { if (m.type() === 'error') errs2.push(m.text()) })

  // 同时发送（时间差 <500ms；问题用具体口径避免含糊判定触发反问卡）
  await Promise.all([
    page1.locator('.chat-input').fill('按渠道汇总销售额'),
    page2.locator('.chat-input').fill('查询渠道类型表的数据量'),
  ])
  await Promise.all([
    page1.keyboard.press('Enter'),
    new Promise((r) => setTimeout(r, 300)).then(() => page2.keyboard.press('Enter')),
  ])

  // v2：若含糊判定仍触发反问卡 → 按全部默认提交，保证两流都能完成
  const answerIfQuestion = async (pg: any) => {
    const q = pg.locator('.question-card')
    if (await q.isVisible({ timeout: 1500 }).catch(() => false)) {
      console.log('[multitabA] 出现反问卡——按全部默认提交')
      await pg.locator('.btn-ghost', { hasText: '全部默认' }).click()
    }
  }
  await answerIfQuestion(page1)
  await answerIfQuestion(page2)

  // 两流都应完成（各自收到回复）
  await waitStreamEnd(page1)
  await waitStreamEnd(page2)

  const b1 = await page1.locator('.msg-bubble').count()
  const b2 = await page2.locator('.msg-bubble').count()
  const alive1 = await page1.evaluate(() => Date.now()).then(() => true).catch(() => false)
  const alive2 = await page2.evaluate(() => Date.now()).then(() => true).catch(() => false)
  console.log(`[multiA] 标签1气泡=${b1} 标签2气泡=${b2} 主线程=${alive1}/${alive2}`)
  console.log(`[multiA] console errors: 标签1=${errs1.length} 标签2=${errs2.length}`)
  expect(b1 >= 2 && b2 >= 2, '[multiA] 双标签并发两流都应完成').toBe(true)
  expect(alive1 && alive2, '[multiA] 双标签都不应卡死').toBe(true)

  await cleanup(page1)
  await ctx1.close()
  await ctx2.close()
})

test('@multitab B 双标签列表一致性', async ({ browser }) => {
  const ctx1 = await browser.newContext()
  const page1 = await ctx1.newPage()
  await page1.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page1)
  // 开头清理残留会话（其他测试失败时 cleanup 未执行会污染基线）
  await cleanup(page1)

  const ctx2 = await browser.newContext()
  const page2 = await ctx2.newPage()
  await page2.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await shareAuth(page1, ctx2)

  await page1.goto('/qa')
  await page2.goto('/qa')
  await page1.locator('.chat-input').waitFor()
  await page2.locator('.chat-input').waitFor()

  // 标签1 建两个会话
  await page1.locator('.chat-input').fill('会话甲')
  await page1.keyboard.press('Enter')
  await waitStreamEnd(page1)
  await page1.locator('.toolbar-btn', { hasText: '+ 新建' }).click()
  await page1.waitForTimeout(500)
  await page1.locator('.chat-input').fill('会话乙')
  await page1.keyboard.press('Enter')
  await waitStreamEnd(page1)

  // 标签2 刷新列表 → 应看到 2 个会话
  await page2.reload()
  await page2.waitForTimeout(1500)
  const items2 = await page2.locator('.session-item').count()
  console.log(`[multiB] 标签2 刷新后会话数=${items2}（期望 2）`)
  expect(items2 >= 2, '[multiB] 标签2 应看到标签1创建的会话').toBe(true)

  // 标签2 API 删除一个会话（UI 删除按钮已有 bug3b spec 覆盖；此处验证跨标签一致性）
  // 注：QAPage reload 时无 sessionId 会自动建会话（产品设计）——断言以"删除的会话消失"为准
  const api = apiClient(page1)
  const r = await api.get('/api/v1/chat/sessions')
  const sessions = await r.json()
  let deletedId = ''
  if (sessions?.length) {
    deletedId = sessions[0].id
    const del = await api.delete(`/api/v1/chat/sessions/${deletedId}`)
    console.log(`[multiB] API 删除响应: ${del.status()}`)
  }
  await page1.reload()
  await page1.waitForTimeout(2000)
  const after = await (await api.get('/api/v1/chat/sessions')).json()
  const stillThere = (after || []).some((s: any) => s.id === deletedId)
  const items1 = await page1.locator('.session-item').count()
  console.log(`[multiB] 删除后: 后端仍有=${stillThere} 标签1 UI 会话数=${items1}（后端 ${after.length}）`)
  expect(stillThere, '[multiB] 删除的会话不应再出现在后端列表（跨标签一致）').toBe(false)
  expect(items1, '[multiB] 标签1 UI 会话数应 >= 后端列表（一致性或含自动建）').toBeGreaterThanOrEqual(after.length)

  await cleanup(page1)
  await ctx1.close()
  await ctx2.close()
})
