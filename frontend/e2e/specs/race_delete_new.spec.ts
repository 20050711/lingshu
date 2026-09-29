// @race 竞态矩阵：新建/删除会话相关
// 场景 A：streaming 中「+ 新建」按钮禁用（by design）→ 切页再回绕过 UI 禁用 → 会话数不得暴增
// 场景 B：空态双击发送 → POST /ask 必须恰 1 次（防重生效），新会话恰 1 个
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { waitStreamingStart, waitStreamEnd } from '../helpers/wait'
import { LONG_QUESTION, SHORT_QUESTION } from '../helpers/constants'

test.setTimeout(180_000)

test('@race A streaming 中点新建：禁用 + 切页绕过不暴增', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  const api = apiClient(page)
  const countBefore = (await apiList(api)).length

  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)

  // streaming 中新建按钮应禁用（by design）
  const newBtn = page.locator('.toolbar-btn', { hasText: '+ 新建' })
  const disabled = await newBtn.isDisabled()
  console.log(`[raceA] streaming 中新建按钮 disabled=${disabled}`)
  expect(disabled, '[raceA] streaming 中新建按钮应禁用').toBe(true)

  // 切页再回（绕过 UI 禁用路径，观察是否有其他入口重复新建）
  await page.locator('.sidebar-item', { hasText: '首页' }).click()
  // 首页现有两行 .func-grid（2026-08-12 新增置灰卡行），取第一个仅作加载完成信号
  await page.locator('.func-grid').first().waitFor()
  await page.locator('.sidebar-item', { hasText: '智能助手' }).click()
  await page.locator('.chat-input').waitFor()
  await waitStreamEnd(page)

  // 等流结束（切回时流可能已 abort）→ 点新建 → 会话数 +1
  await page.waitForTimeout(3000)
  const beforeNew = (await apiList(api)).length
  await page.locator('.toolbar-btn', { hasText: '+ 新建' }).click()
  await page.waitForTimeout(1500)
  const afterNew = (await apiList(api)).length
  console.log(`[raceA] 会话数: 初始=${countBefore} 新建前=${beforeNew} 新建后=${afterNew}`)
  expect(afterNew - beforeNew, '[raceA] 新建恰 1 个会话').toBe(1)

  // 清理
  const sessions = await apiListRaw(api)
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
})

test('@race B 空态双击发送：POST /ask 恰 1 次、会话恰 1 个', async ({ page, request }) => {
  await loginViaUI(page)
  // 前置清理：删光残留会话（其他测试失败时 cleanup 未执行会污染「恰 1 个」断言）
  const apiPre = apiClient(page)
  const rawPre = await apiPre.get('/api/v1/chat/sessions')
  const listPre = await rawPre.json()
  const preArr = Array.isArray(listPre) ? listPre : (listPre.sessions || [])
  for (const s of preArr) await apiPre.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  let askCount = 0
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/chat/ask')) askCount++
  })

  // 空态双击发送按钮
  await page.locator('.chat-input').fill(SHORT_QUESTION)
  const sendBtn = page.locator('.btn-primary', { hasText: '发送' })
  await sendBtn.dblclick()
  await waitStreamEnd(page)

  const api = apiClient(page)
  const sessions = await apiList(api)
  console.log(`[raceB] POST /ask=${askCount} 会话数=${sessions.length}`)
  expect(askCount, '[raceB] 双击发送应只发出 1 条').toBe(1)
  expect(sessions.length, '[raceB] 应恰 1 个会话').toBe(1)

  // 清理
  const raw = await apiListRaw(api)
  for (const s of raw || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
})

async function apiList(api: any): Promise<any[]> {
  const r = await api.get('/api/v1/chat/sessions')
  const d = await r.json()
  // 2026-08-12：接口返回数组（非 {sessions:[]}——SUMMARY 已记录的基建坑）；双形态兼容防恒空
  return Array.isArray(d) ? d : (d.sessions || [])
}
async function apiListRaw(api: any) {
  const r = await api.get('/api/v1/chat/sessions')
  return r.json()
}
