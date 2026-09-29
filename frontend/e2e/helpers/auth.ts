// 登录 helper：UI 三要素登录（乱操作主路径）+ API 直登（后端核实，独立 cookie context）
import { expect, type Page, type APIRequestContext } from '@playwright/test'
import { CHAOS } from './constants'

/**
 * UI 登录（随机操作测试 chaos01）。
 * 出现验证码 = 防爆破计数污染（auth:fail:127.0.0.1 有残留）→ 明确报错并给解锁命令。
 * 约定：密码永不错输（E2E_PLAN.md 既有约定），登录失败立即中止并提示。
 */
export async function loginViaUI(page: Page) {
  await page.goto('/login')
  await page.locator('.login-card').waitFor()
  // 1. 部门下拉（AntD Select，options 为 {value: dept_id, label: name}）
  // 先等 departments 接口返回，再点开下拉（options 加载完成才渲染 option）
  await page.waitForResponse((r) => r.url().includes('/auth/departments'), { timeout: 15_000 }).catch(() => {})
  const select = page.locator('.login-card .ant-select')
  await select.click()
  // 真实选项是 .ant-select-item-option（title=部门名）；[role="option"] 是无障碍 aria 层（仅前 2 个）不适用
  const option = page.locator('.ant-select-item-option', { hasText: CHAOS.deptName }).first()
  await option.click({ timeout: 10_000 })
  // 2. 账号 + 密码
  await page.locator('.login-input').nth(0).fill(CHAOS.username)
  await page.locator('.login-input').nth(1).fill(CHAOS.password)
  // 3. 登录（employee → /home）
  const captcha = page.locator('input[placeholder="验证码"]')
  await page.locator('.login-btn').click()
  try {
    await page.waitForURL('**/home', { timeout: 15_000 })
  } catch {
    const captchaVisible = await captcha.isVisible().catch(() => false)
    if (captchaVisible) {
      throw new Error(
        '登录出现验证码（防爆破计数污染）——请执行: redis-cli del auth:fail:127.0.0.1 后重试',
      )
    }
    throw new Error(`登录失败/超时（账号 ${CHAOS.username} 或服务异常）`)
  }
}

/**
 * API 直登（后端核实用；cookie 自动保存在该 request context，后续请求自带鉴权）。
 */
export async function apiLogin(request: APIRequestContext) {
  const r = await request.post('/api/v1/auth/login', {
    data: {
      department_id: CHAOS.deptId,
      username: CHAOS.username,
      password: CHAOS.password,
    },
  })
  if (!r.ok()) throw new Error(`API 登录失败 ${r.status()}: ${await r.text()}`)
  return r
}

/** 登录后断言（helper 复用的最小冒烟：验证登录态可用） */
export async function expectLoggedIn(page: Page) {
  await expect(page).toHaveURL(/\/home$/, { timeout: 15_000 })
}

/**
 * 跨 context 共享登录态（2026-08-13 单点登录适配）。
 * 双 context 场景原做法「两个 context 各自 UI 登录同账号」在单点登录下互踢：
 * 第二次登录 token_version+1 使第一个 context 的 token 立即失效。
 * 正确做法：第一个 context 登录后，把 cookie 与 localStorage user 复制给第二个 context，
 * 两 context 共享同一 token（无新登录、无互踢），等价于「同一浏览器两个标签」。
 */
export async function shareAuth(pageFrom: Page, ctxTo: import('@playwright/test').BrowserContext) {
  const cookies = await pageFrom.context().cookies()
  await ctxTo.addCookies(cookies)
  const userJson = await pageFrom.evaluate(() => localStorage.getItem('user')).catch(() => null)
  if (userJson) {
    // 一次性注入（sessionStorage 哨兵）：只在每个标签首次导航时写入 user。
    // 若每次导航都重注入，被踢页面登录页 mount 永远读到 user → 自动跳回 /home →
    // 再 401 再跳回，形成无限弹跳（真实用户无此问题——handleAuthExpired 清 user 后即止）。
    await ctxTo.addInitScript((u: string) => {
      if (!sessionStorage.getItem('auth_shared')) {
        sessionStorage.setItem('auth_shared', '1')
        localStorage.setItem('user', u)
      }
    }, userJson)
  }
}
