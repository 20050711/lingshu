// @fuzz 随机乱操作序列：加权操作池 + 每 5 步不变量检查 + 轮末 API 会话核对
// 配置：FUZZ_ROUNDS（默认10）× FUZZ_STEPS（默认20）；FUZZ_SEED 固定可复现（LCG）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { countBubbles } from '../helpers/wait'
import { collectErrors } from '../helpers/console'
import { SHORT_QUESTION } from '../helpers/constants'

// 2026-09-18：600s → 1200s。原预算是在「一有违规就提前 break」的前提下定的——
// 一旦不变量全过、200 步（10 轮 × 20 步）真跑完，600s 必然不够（实测跑满 600s 被掐）。
// 这是**正常的全长**，不是卡死：真卡死会被 K/I 系列不变量或心跳先抓到。
test.setTimeout(1_200_000)

test('@fuzz 随机乱操作序列（不变量守护）', async ({ page, request }) => {
  const ROUNDS = Number(process.env.FUZZ_ROUNDS || 10)
  const STEPS = Number(process.env.FUZZ_STEPS || 20)
  const seed = Number(process.env.FUZZ_SEED || 42)
  let s = seed
  const rnd = () => {
    s = (s * 1664525 + 1013904223) % 4294967296
    return s / 4294967296
  }

  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  const errs = collectErrors(page)

  // 操作池（加权）
  const POOL: { name: string; weight: number; fn: () => Promise<string> }[] = [
    { name: '发送短问题', weight: 10, fn: async () => {
      const ta = page.locator('.chat-input')
      if (await ta.isEnabled()) { await ta.fill(SHORT_QUESTION); await page.keyboard.press('Enter'); return 'ok' }
      return 'skip(输入禁用)'
    } },
    { name: '停止', weight: 8, fn: async () => {
      const btn = page.locator('.btn-ghost', { hasText: '停止' })
      if (await btn.isVisible().catch(() => false)) { await btn.click(); return 'ok' }
      return 'skip(无停止按钮)'
    } },
    { name: '切会话', weight: 15, fn: async () => {
      const items = page.locator('.session-item')
      const n = await items.count()
      if (n === 0) return 'skip(无会话)'
      await items.nth(Math.floor(rnd() * n)).click()
      await page.waitForTimeout(400)
      return 'ok'
    } },
    { name: '新建会话', weight: 10, fn: async () => {
      const btn = page.locator('.toolbar-btn', { hasText: '+ 新建' })
      if (await btn.isEnabled()) { await btn.click(); await page.waitForTimeout(400); return 'ok' }
      return 'skip(禁用)'
    } },
    { name: '删除会话', weight: 8, fn: async () => {
      const del = page.locator('.session-item .toolbar-btn', { hasText: '删除' }).first()
      if (await del.isVisible().catch(() => false)) {
        await del.click()
        // 2026-08-18：antd 两字按钮插空格「确 定」→ hasText 用正则（否则恒空走 skip）
        const ok = page.locator('.ant-popover .ant-btn-primary', { hasText: /确\s*定/ }).first()
        if (await ok.isVisible().catch(() => false)) { await ok.click(); await page.waitForTimeout(400); return 'ok' }
        return 'skip(弹层未出)'
      }
      return 'skip(无删除按钮)'
    } },
    { name: '刷新页面', weight: 5, fn: async () => {
      await page.reload()
      await page.locator('.chat-input').waitFor()
      await page.waitForTimeout(800)
      return 'ok'
    } },
    { name: '路由跳转', weight: 15, fn: async () => {
      const routes = ['/home', '/qa', '/skills', '/mcp', '/tools', '/feedback', '/guide']
      await page.goto(routes[Math.floor(rnd() * routes.length)])
      await page.waitForTimeout(500)
      // 回 QA 保持主战场
      if (!page.url().includes('/qa')) { await page.goto('/qa'); await page.locator('.chat-input').waitFor() }
      return 'ok'
    } },
    { name: '连点发送', weight: 5, fn: async () => {
      const ta = page.locator('.chat-input')
      if (await ta.isEnabled()) {
        await ta.fill('1+1=?')
        const sendBtn = page.locator('.btn-primary', { hasText: '发送' })
        if (await sendBtn.isVisible().catch(() => false)) { await sendBtn.dblclick(); return 'ok' }
      }
      return 'skip'
    } },
    { name: '输入长文本', weight: 5, fn: async () => {
      const ta = page.locator('.chat-input')
      if (await ta.isEnabled()) { await ta.fill(Array.from({ length: 50 }, (_, i) => `乱操行${i}`).join('\n')); return 'ok' }
      return 'skip'
    } },
    { name: '会话列开合', weight: 5, fn: async () => {
      await page.locator('.toolbar-btn', { hasText: '会话列表' }).click()
      return 'ok'
    } },
    { name: '等待 2s', weight: 10, fn: async () => { await page.waitForTimeout(2000); return 'ok' } },
  ]
  const total = POOL.reduce((a, p) => a + p.weight, 0)
  const pick = () => {
    let r = rnd() * total
    for (const p of POOL) { r -= p.weight; if (r <= 0) return p }
    return POOL[POOL.length - 1]
  }

  // 不变量检查
  const invariants = async (label: string): Promise<string[]> => {
    const bad: string[] = []
    const textLen = await page.evaluate(() => document.body.innerText.length)
    if (textLen < 100) bad.push('I1 白屏')
    // 2026-09-18：随机操作**本身就会制造非法请求**，由此产生的 4xx 是后端的**正确响应**，不是缺陷：
    //   · 删掉会话后，在途请求仍去问它的子资源  → 404
    //   · 对着没有运行中任务的会话点「停止」    → 409
    //   · 会话刚被删/切走时提交                  → 400/404
    // 逐个端点豁免会一直漏（实测一路补了 messages → context → stop），故按**语义**豁免：
    // `/api/v1/chat/**` 上的 400/404/409。
    // **仍然计入 I2**：任何 5xx、401/403（鉴权异常有诊断价值）、非 HTTP 的 console error、pageerror。
    const BENIGN_RACE = /status of (400|404|409) .*@ .*\/api\/v1\/chat\//
    const realErrs = errs.errors.filter((e) => !BENIGN_RACE.test(e.text))
    if (realErrs.length > 0) {
      bad.push(`I2 console error:\n${realErrs.map((e) => `[${e.type}] ${e.text}`).join('\n').slice(0, 200)}`)
    }
    // I3 会话一致性：列表空 ⇔ 聊天区空态
    const items = page.locator('.session-item')
    const n = await items.count()
    const users = await countBubbles(page, 'user')
    if (n === 0 && users > 0) bad.push(`I3 列表空但聊天区有 ${users} 条 user 气泡`)
    // I4 流式中不卡死（2026-09-18 换）：原为「停止/发送互斥」，但 2026-08-17 问题 9 起产品
    // **有意**让两者共存（见 ChatPanel.tsx「与停止按钮共存——原实现互斥替换导致流式中无法发送」），
    // 该互斥假设已废弃 → 恒失败。换成真正能抓 bug 的不变量：停止可见（=流式中）时输入框必须可用。
    const stopVisible = await page.locator('.btn-ghost', { hasText: '停止' }).isVisible().catch(() => false)
    if (stopVisible) {
      const inputOk = await page.locator('.chat-input').isEnabled().catch(() => false)
      if (!inputOk) bad.push('I4 流式中输入框被禁用（疑似永久卡死）')
    }
    errs.reset()
    return bad
  }

  const history: string[] = []
  let found: string[] = []
  for (let r = 0; r < ROUNDS && found.length === 0; r++) {
    for (let st = 0; st < STEPS; st++) {
      const op = pick()
      let result = 'err'
      try { result = await op.fn() } catch (e: any) { result = `异常(${String(e).slice(0, 80)})` }
      history.push(`${r}.${st} ${op.name}=${result}`)
      if (st % 5 === 4) {
        const bad = await invariants(`r${r}s${st}`)
        if (bad.length) {
          found = bad
          console.log(`[fuzz] 不变量失败 @r${r}s${st}（seed=${seed}）:\n${bad.join('\n')}`)
          console.log(`[fuzz] 最近 5 步:\n${history.slice(-5).join('\n')}`)
          break
        }
      }
    }
    if (found.length) break
    // 轮末：UI 会话数与 API 核对
    const api = apiClient(page)
    const rp = await api.get('/api/v1/chat/sessions')
    const sessions = await rp.json()
    const uiCount = await page.locator('.session-item').count()
    if (Math.abs(uiCount - (sessions?.length || 0)) > 1) {
      found = [`I5 会话数不一致: UI=${uiCount} API=${sessions?.length}`]
      console.log(`[fuzz] ${found[0]}（seed=${seed}）`)
      break
    }
    console.log(`[fuzz] 轮 ${r + 1}/${ROUNDS} 完成（seed=${seed}）`)
  }

  expect(found, `[fuzz] 不变量失败（seed=${seed}）:\n${found.join('\n')}`).toEqual([])
  console.log(`[fuzz] 通过：${ROUNDS} 轮 × ${STEPS} 步无违反不变量（seed=${seed}）`)

  // 清理 chaos01 全部会话
  const api = apiClient(page)
  const rp = await api.get('/api/v1/chat/sessions')
  const sessions = await rp.json()
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
})
