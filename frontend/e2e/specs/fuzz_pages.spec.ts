// @fuzzpages 全页面随机乱操作（单点/并发多点/连点/混合）+ 卡死检测
// 每个业务页面做 FUZZ_PAGE_STEPS（默认 12）次操作，操作类型加权随机：
//   single 单点 / multi 同时点 2-3 个按钮 / rapid 同一按钮 3 连点 / clickType 点击+输入 /
//   clickNav 点击按钮同时导航 / (QA 页) sendStop 发送后立即停止、sendSwitch 发送中切会话
// 每次操作后检测「卡死」三指标：
//   K1 主线程心跳：page.evaluate 5s 内不返回 = 主线程阻塞（卡死）
//   K2 白屏：body innerText < 100
//   K3 永久 loading：连续 3 次检查 .ant-spin 仍可见 = 卡 loading
// 任一卡死 → 记录页面 + 最近操作栈 → FAIL（bug 确认态）
// 配置：FUZZ_PAGE_STEPS（默认 12）；纳入默认测试（run-e2e.sh 无 GREP 全跑即包含）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { collectErrors } from '../helpers/console'
import { SHORT_QUESTION } from '../helpers/constants'

test.setTimeout(900_000)

const PAGES: [string, string][] = [
  ['/home', '首页'],
  ['/qa', '智能助手'],
  ['/skills', 'AI技能管理'],
  ['/skills/default', '技能-默认'],
  ['/skills/dept', '技能-部门'],
  ['/mcp', 'AI外部工具'],
  ['/feedback', '反馈'],
  ['/guide', '使用说明'],
  ['/tools', '定制化工具'],
  ['/tools/resume', '简历初筛'],
  ['/tools/kb', '知识库'],
  ['/ceo', 'CEO看板(守卫→home)'],
  ['/admin', '管理后台(守卫→home)'],
]

test('@fuzzpages 全页面随机乱操作（含并发多点）：卡死检测', async ({ page }) => {
  const STEPS = Number(process.env.FUZZ_PAGE_STEPS || 12)
  const seed = Number(process.env.FUZZ_SEED || 42)
  let s = seed
  const rnd = () => {
    s = (s * 1664525 + 1013904223) % 4294967296
    return s / 4294967296
  }
  const pickIdx = (n: number) => Math.floor(rnd() * n)

  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  const errs = collectErrors(page)

  // ---- 卡死检测三指标 ----
  const checkAlive = async (): Promise<boolean> => {
    const alive = await Promise.race([
      page.evaluate(() => Date.now()).then(() => true).catch(() => false),
      new Promise<boolean>((r) => setTimeout(() => r(false), 5000)),
    ])
    return alive
  }
  const checkWhite = async (): Promise<boolean> => {
    const len = await page.evaluate(() => document.body.innerText.length).catch(() => 0)
    return len < 100
  }
  const checkSpin = async (): Promise<boolean> => {
    for (let i = 0; i < 3; i++) {
      const spin = await page.locator('.ant-spin:visible').count().catch(() => 0)
      if (spin === 0) return false
      await page.waitForTimeout(1000)
    }
    return true
  }

  // ---- 操作原语 ----
  // 排除「退出」（sidebar「退出登录」+ header「退出」按钮）：登出导致后续页面 401 白屏误报
  // （登出场景由 race_auth_network 专门覆盖）
  const visibleTargets = () =>
    page.locator(
      'button:visible:not(:has-text("退出")), a:visible, .sidebar-item:visible:not(:has-text("退出")), .ant-switch:visible, [class*="gd-card"]:visible, [role="radio"]:visible',
    )
  const pickTargets = async (k: number) => {
    const t = visibleTargets()
    const n = await t.count().catch(() => 0)
    if (n === 0) return []
    const chosen = new Set<number>()
    while (chosen.size < Math.min(k, n)) chosen.add(pickIdx(n))
    return Array.from(chosen).map((i) => t.nth(i))
  }
  const safeClick = (el: any) => el.click({ timeout: 3000 }).catch(() => {})

  const OPS: { name: string; weight: number; fn: () => Promise<string> }[] = [
    // 单点
    { name: 'single', weight: 25, fn: async () => {
      const els = await pickTargets(1)
      if (!els.length) return 'skip(无可点)'
      await safeClick(els[0]); return 'ok'
    } },
    // 并发多点：同时点 2-3 个按钮（真实并发点击）
    { name: 'multi', weight: 30, fn: async () => {
      const els = await pickTargets(2 + pickIdx(2))
      if (els.length < 2) return 'skip(元素不足)'
      await Promise.all(els.map(safeClick))
      return `并发点 ${els.length} 个`
    } },
    // 连点：同一目标 3 连点（间隔 50ms）
    { name: 'rapid', weight: 15, fn: async () => {
      const els = await pickTargets(1)
      if (!els.length) return 'skip(无可点)'
      const el = els[0]
      for (let i = 0; i < 3; i++) { await safeClick(el); await page.waitForTimeout(50) }
      return '3 连点'
    } },
    // 点击 + 输入框同时操作
    { name: 'clickType', weight: 12, fn: async () => {
      const els = await pickTargets(1)
      const ta = page.locator('textarea:visible, input:visible:not([type="hidden"])').first()
      if (!els.length || (await ta.count().catch(() => 0)) === 0) return 'skip'
      await Promise.all([safeClick(els[0]), ta.fill('并发输入').catch(() => {})])
      return '点击+输入'
    } },
    // 点击按钮同时导航（点击 + 路由跳转并发）
    { name: 'clickNav', weight: 10, fn: async () => {
      const els = await pickTargets(1)
      if (!els.length) return 'skip(无可点)'
      const nav = page.locator('.sidebar-item:visible').first()
      const navVisible = (await nav.count().catch(() => 0)) > 0
      await Promise.all([
        safeClick(els[0]),
        navVisible ? nav.click({ timeout: 3000 }).catch(() => {}) : Promise.resolve(),
      ])
      // 若被导航走了，等回到本页场景（导航可能到其他页，属正常乱操作）
      await page.waitForTimeout(600)
      return '点击+导航'
    } },
  ]
  // QA 页并发特殊操作（发送类）
  const QA_OPS: { name: string; fn: () => Promise<string> }[] = [
    { name: 'sendStop', fn: async () => {
      const ta = page.locator('.chat-input')
      if (!(await ta.isEnabled())) return 'skip'
      await ta.fill(SHORT_QUESTION)
      await page.keyboard.press('Enter')
      await page.waitForTimeout(300)
      const stop = page.locator('.btn-ghost', { hasText: '停止' })
      await safeClick(stop.first())
      return '发送后立即停止'
    } },
    { name: 'sendSwitch', fn: async () => {
      const ta = page.locator('.chat-input')
      const items = page.locator('.session-item:visible')
      if (!(await ta.isEnabled()) || (await items.count().catch(() => 0)) === 0) return 'skip'
      await ta.fill(SHORT_QUESTION)
      await page.keyboard.press('Enter')
      await page.waitForTimeout(300)
      // 发送中同时切会话 + 点停止（三重并发）
      const stop = page.locator('.btn-ghost', { hasText: '停止' })
      await Promise.all([safeClick(items.first()), safeClick(stop.first())])
      return '发送中切会话+停止'
    } },
  ]
  const totalW = OPS.reduce((a, o) => a + o.weight, 0)
  const pickOp = (isQA: boolean) => {
    if (isQA && rnd() < 0.3) return QA_OPS[pickIdx(QA_OPS.length)]
    let r = rnd() * totalW
    for (const o of OPS) { r -= o.weight; if (r <= 0) return o }
    return OPS[OPS.length - 1]
  }

  const failures: string[] = []
  const perPage: string[] = []

  for (const [path, label] of PAGES) {
    await page.goto(path)
    await page.waitForTimeout(800)
    // 兜底：若乱操作意外登出（401 踢回 /login）→ 重新登录再继续
    if (page.url().includes('/login')) {
      console.log(`[fuzzpages] ${label} 登录态丢失，重新登录`)
      await loginViaUI(page)
    }
    const pageOps: string[] = []
    let pageDead = ''

    for (let i = 0; i < STEPS; i++) {
      const op = pickOp(path === '/qa')
      let result = '异常'
      try { result = await op.fn() } catch (e: any) { result = `异常(${String((e && e.message) || e).slice(0, 40)})` }
      pageOps.push(`s${i}:${op.name}=${result}`)
      await page.waitForTimeout(400)

      if (i % 2 === 1) {
        // 2026-09-18：**循环中途**被踢（chaos01 单点登录，其他上下文一登就顶掉）会跳到 /login，
        // 此时页面"白"是登出的结果、不是卡死——先重登再判 K2。
        // 原来只在**每页开始前**兜底重登，循环内一被踢就误报"白屏卡死"（本次实测就栽在这）。
        if (page.url().includes('/login')) {
          console.log(`[fuzzpages] ${label} 中途登录态丢失，重新登录后继续`)
          await loginViaUI(page)
          await page.goto(path)
          await page.waitForTimeout(800)
          continue
        }
        if (!(await checkAlive())) { pageDead = 'K1 主线程心跳失败（卡死）'; break }
        if (await checkWhite()) { pageDead = 'K2 白屏（innerText<100）'; break }
        if (await checkSpin()) { pageDead = 'K3 永久 loading（ant-spin 3s 不消失）'; break }
      }
    }

    if (!pageDead && !(await checkAlive())) pageDead = 'K1 收尾心跳失败'
    if (pageDead) {
      failures.push(`[${label}] ${path}: ${pageDead}`)
      console.log(`[fuzzpages] ❌ ${label} 卡死确认: ${pageDead}`)
      console.log(`[fuzzpages]   ${label} 最近操作: ${pageOps.slice(-5).join(' | ')}`)
    } else {
      console.log(`[fuzzpages] ✅ ${label} ${STEPS} 次操作（含并发）无卡死（seed=${seed}）`)
    }
    perPage.push(`${label}: ${pageDead ? '❌ ' + pageDead : '✅'}`)

    const errText = errs.describe()
    if (errText) console.log(`[fuzzpages] ${label} console/pageerror:\n${errText.slice(0, 300)}`)
    errs.reset()
  }

  console.log(`[fuzzpages] 汇总（seed=${seed}）:\n${perPage.join('\n')}`)
  expect(failures, `[fuzzpages] 存在卡死页面:\n${failures.join('\n')}`).toEqual([])
})
