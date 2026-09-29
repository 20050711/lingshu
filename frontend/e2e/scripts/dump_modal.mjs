import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5174'
const CHAOS = { deptId: 'chaos', deptName: '随机操作测试', username: 'chaos01', password: process.env.E2E_CHAOS_PASSWORD ?? '' }
const b = await chromium.launch({ args: ['--no-sandbox'] })
const ctx = await b.newContext({ baseURL: BASE })
const page = await ctx.newPage()
const t0 = Date.now()
const ts = () => `${((Date.now() - t0) / 1000).toFixed(1)}s`
page.on('pageerror', (e) => console.log(ts(), 'PAGEERROR', String(e).slice(0, 300)))
await page.goto('/login'); await page.locator('.login-card').waitFor()
await page.locator('.login-card .ant-select').click()
await page.locator('.ant-select-item-option', { hasText: CHAOS.deptName }).first().click({ timeout: 10_000 })
await page.locator('.login-input').nth(0).fill(CHAOS.username)
await page.locator('.login-input').nth(1).fill(CHAOS.password)
await page.locator('.login-btn').click()
await page.waitForURL('**/home', { timeout: 15_000 })
await page.evaluate(async () => {
  const r = await fetch('/api/v1/chat/sessions', { credentials: 'include' })
  const list = await r.json()
  for (const s of list) await fetch(`/api/v1/chat/sessions/${s.id}`, { method: 'DELETE', credentials: 'include' })
})
await page.goto('/qa'); await page.locator('.chat-input').waitFor()
await page.locator('.chat-input').fill('你好'); await page.keyboard.press('Enter')
console.log(ts(), '快速轮发送')
await page.locator('.btn-ghost', { hasText: '停止' }).waitFor({ timeout: 60_000 }).catch(() => console.log(ts(), 'warn: 停止按钮未出现'))
console.log(ts(), '流式已开始')
await page.waitForFunction(() => {
  const stop = [...document.querySelectorAll('.btn-ghost')].some((x) => x.textContent.includes('停止'))
  return !stop
}, { timeout: 150_000 }).catch(() => console.log(ts(), 'warn: 停止按钮未消失'))
console.log(ts(), '快速轮结束')
const mask = page.locator('.sessions-mask')
if (await mask.count()) await mask.click({ position: { x: 500, y: 300 } }).catch(() => {})
await page.locator('.mode-btn', { hasText: '复杂任务' }).click()
const upgradeModal = page.locator('.ant-modal-confirm', { hasText: '升级为复杂任务模式' })
await upgradeModal.waitFor({ timeout: 15_000 }).catch(() => console.log(ts(), 'warn: 升级 Modal 未出现'))
console.log(ts(), '升级 Modal 可见')
// 点击升级前 dump
await page.locator('.ant-modal-confirm button', { hasText: /升\s*级/ }).click()
console.log(ts(), '点击升级')
await page.waitForTimeout(800)
// 升级后立即 dump modal 状态
const dump = () => page.evaluate(() => [...document.querySelectorAll('.ant-modal-wrap')].map((w) => {
  const r = w.getBoundingClientRect(); const s = getComputedStyle(w)
  return { rect: [r.x, r.y, r.width, r.height].map(Math.round), display: s.display, pe: s.pointerEvents, z: s.zIndex, cls: w.className, text: (w.textContent || '').slice(0, 150) }
}))
console.log(ts(), '升级后 wraps:', JSON.stringify(await dump()))
// 等反问卡
const q = page.locator('.question-card')
await q.waitFor({ timeout: 90_000 }).catch(() => console.log(ts(), 'NO_QCARD'))
console.log(ts(), '反问卡出现')
console.log(ts(), '反问时 wraps:', JSON.stringify(await dump(), null, 1))
const btn = page.locator('.btn-ghost', { hasText: '全部默认' })
const clickResult = await btn.click({ timeout: 5000 }).then(() => 'OK').catch((e) => String(e).split('\n')[0])
console.log(ts(), 'CLICK:', clickResult)
await page.waitForTimeout(1500)
console.log(ts(), '点击后 wraps:', JSON.stringify(await dump()))
await page.screenshot({ path: '/tmp/dump_modal_final.png' })
console.log(ts(), 'screenshot /tmp/dump_modal_final.png')
await b.close()
