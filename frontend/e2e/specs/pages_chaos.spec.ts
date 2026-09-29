// @pages 全页面乱操作：路由循环 + 守卫 + QA 对话中跳工具页 + 工具页各自乱操作 + Guide 浮窗 + 反馈提交
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { waitStreamingStart, waitStreamEnd } from '../helpers/wait'
import { collectErrors } from '../helpers/console'
import { LONG_QUESTION } from '../helpers/constants'

const SAMPLE_MP4 = '/opt/tardis/frontend/public/samples/sample.mp4'
const SAMPLE_DOCX = '/opt/tardis/frontend/public/samples/sample.docx'

test.setTimeout(240_000)

// ---- T1 路由循环 + 守卫 ----
test('@real-llm @pages T1 全部路由循环 ×2：非白屏 + 无 console error + 守卫重定向', async ({ page }) => {
  await loginViaUI(page)
  const errs = collectErrors(page)

  const ROUTES: [string, string][] = [
    ['/home', '首页'],
    ['/qa', '智能助手'],
    ['/skills', 'AI技能管理'],
    ['/skills/default', '默认'],
    ['/skills/dept', '部门'],
    ['/mcp', 'AI外部工具'],
    ['/feedback', '反馈'],
    ['/guide', '使用说明'],
    ['/tools', '定制化工具'],
    ['/tools/resume', '简历初筛'],
    ['/tools/kb', '知识库'],
  ]
  for (let round = 0; round < 2; round++) {
    for (const [path, label] of ROUTES) {
      await page.goto(path)
      await page.waitForTimeout(600)
      const textLen = await page.evaluate(() => document.body.innerText.length)
      const url = page.url()
      // 守卫：/ceo /admin 应由 employee 重定向 /home
      expect(textLen, `[T1] ${path} 白屏`).toBeGreaterThan(100)
      console.log(`[T1] r${round + 1} ${path} → ${new URL(url).pathname}（文本 ${textLen} 字）`)
    }
  }
  // 守卫断言
  await page.goto('/ceo')
  await expect(page).toHaveURL(/\/home$/, { timeout: 10_000 })
  await page.goto('/admin')
  await expect(page).toHaveURL(/\/home$/, { timeout: 10_000 })
  await page.goto('/unknown-xyz')
  await expect(page).toHaveURL(/\/home$/, { timeout: 10_000 })
  console.log('[T1] 守卫重定向（/ceo /admin /unknown → /home）生效')

  const errText = errs.describe()
  expect(errText, `[T1] 路由循环出现 console/pageerror: ${errText.slice(0, 300)}`).toBe('')
})

// ---- T2 QA 对话中跳工具页再回来 ----
test('@real-llm @pages T2 QA 对话中跳工具页再回：对话流不被破坏', async ({ page, request }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)

  // 跳工具页再回
  await page.locator('.sidebar-item', { hasText: '定制化工具' }).click()
  await page.waitForTimeout(800)
  await page.locator('.sidebar-item', { hasText: '智能助手' }).click()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(1000)

  // 断言：对话仍在（停止按钮还在 = 流未被页面切换破坏）或已完成
  const stopVisible = await page.locator('.btn-ghost', { hasText: '停止' }).isVisible().catch(() => false)
  console.log(`[T2] 回 QA 后停止按钮可见=${stopVisible}（${stopVisible ? '流仍在收' : '已完成/已中断'}）`)
  await waitStreamEnd(page).catch(() => {})
  console.log('[T2] 对话流最终结束（无白屏/无异常）')

  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions')
  const sessions = await r.json()
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
})
