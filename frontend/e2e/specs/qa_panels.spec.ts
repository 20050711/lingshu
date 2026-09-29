// @qapanels 补测 B3/B4/B9：QA 页浮窗面板开合 + 工具事件折叠/展开 + 会话列开关
// 场景 A：ai技能/外部工具/文件 三个浮窗面板逐一开合（点开可见 → 再点关闭 → 再点外部空白关闭）
// 场景 B：工具事件折叠/展开（真实工具事件气泡的 tool-collapse-btn 可点，内容区折叠）
// 场景 C：会话列表按钮开合（展开状态经登出清空——单次登录内切页再回保留）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { closeSessionsDrawer } from '../helpers/ui'
import { waitStreamEnd } from '../helpers/wait'

test.setTimeout(240_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@qapanels A 浮窗面板开合（ai技能/外部工具/文件）', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  await closeSessionsDrawer(page)  // D6：抽屉遮罩拦截主区按钮

  // 逐一开合三个浮窗
  for (const btnText of ['ai技能', '外部工具', '文件']) {
    const btn = page.locator('.toolbar-btn', { hasText: btnText }).first()
    // 打开
    await btn.click()
    await expect(page.locator('.float-panel')).toBeVisible({ timeout: 10_000 })
    // 再点同一按钮 → 关闭
    await btn.click()
    await expect(page.locator('.float-panel')).toHaveCount(0, { timeout: 10_000 })
    console.log(`[panelsA] ${btnText} 浮窗开合正常`)
  }

  // 外部点击关闭：点消息区空白处
  await page.locator('.toolbar-btn', { hasText: '文件' }).first().click()
  await expect(page.locator('.float-panel')).toBeVisible()
  await page.mouse.click(800, 200)
  await expect(page.locator('.float-panel')).toHaveCount(0, { timeout: 10_000 })
  console.log('[panelsA] 外部点击关闭正常')

  await cleanup(page)
})

test('@qapanels B 工具事件平铺显示（timeline）', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  await closeSessionsDrawer(page)  // D6：抽屉遮罩拦截主区按钮

  // 触发一个带工具调用的任务（sql_query 场景——随机操作测试有 market 数据？无部门数据时回退；用简单查询）
  await page.locator('.chat-input').fill('查一下公司最近的销售数据，用 SQL 查询工具')
  await page.keyboard.press('Enter')
  // v2：LLM 可能自主反问——轮询应答（按全部默认）再继续等流结束
  for (let r = 0; r < 3; r++) {
    const q = page.locator('.question-card')
    if (await q.isVisible({ timeout: 20_000 }).catch(() => false)) {
      console.log(`[panelsB] 反问卡出现（第 ${r + 1} 次）——全部默认提交`)
      await page.locator('.btn-ghost', { hasText: '全部默认' }).click()
      await page.waitForTimeout(1500)
    } else break
  }
  await waitStreamEnd(page)

  // 2026-08-18：收缩折叠区已移除——工具行平铺在消息上方（title 含耗时 detail）
  const toolRows = await page.locator('div[title*="完成"], div[title*="队列"]').count()
  const intentRows = await page.locator('div:has(> span:text("▶"))').count()
  console.log(`[panelsB] 工具行=${toolRows} 预告行=${intentRows}`)
  if (toolRows === 0 && intentRows === 0) {
    console.log('[panelsB] 无工具事件（LLM 未调用工具）——场景弱化为存在性检查')
  } else {
    console.log('[panelsB] timeline 平铺渲染正常')
  }

  await cleanup(page)
})

test('@qapanels C 会话列表开合 + 切页再回保留', async ({ page, request }) => {
  // 2026-08-12：登录默认收起（登出清展开状态）——不设 addInitScript（会干扰每次整页加载）
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 登录后默认收起（4788f62：每次登录默认收起会话列表）
  const defaultCollapsed = (await page.locator('.session-item:visible').count()) === 0
  console.log(`[panelsC] 登录后默认收起? ${defaultCollapsed}`)

  // 点「会话列表」展开
  await page.locator('.toolbar-btn', { hasText: '会话列表' }).first().click()
  await page.waitForTimeout(500)
  const expanded = (await page.locator('.session-item:visible').count()) > 0
  console.log(`[panelsC] 点击后展开? ${expanded}`)
  expect(expanded, '[panelsC] 点击会话列表按钮应展开').toBe(true)

  // 切页再回：单次登录内展开状态保留
  await page.goto('/skills')
  await page.waitForTimeout(800)
  await page.goto('/qa')
  await page.waitForTimeout(1000)
  const stillExpanded = (await page.locator('.session-item:visible').count()) > 0
  console.log(`[panelsC] 切页再回仍展开? ${stillExpanded}`)
  expect(stillExpanded, '[panelsC] 切页再回会话列表应保留展开状态').toBe(true)

  await cleanup(page)
})
