// 复现验证（2026-08-27）：登录后无会话直接上传 → 文件区应立即显示（原 bug：自动建会话后
// 组件闭包 sessionId 未更新，loadFiles 跳过 → 第一次上传不显示，第二次才一起出现）
import { chromium } from 'playwright'
import { mkdtempSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

const BASE = 'http://127.0.0.1:24425'
const USER = { deptId: 'market', username: 'walkthrough', password: 'Py@sqLPELI%kY5ot' }

// 两个小 txt 作为上传文件
const dir = mkdtempSync(join(tmpdir(), 'up-'))
const fA = join(dir, '文件A.txt')
const fB = join(dir, '文件B.txt')
writeFileSync(fA, '内容A')
writeFileSync(fB, '内容B')

const b = await chromium.launch({ args: ['--no-sandbox', '--disable-background-timer-throttling'] })
const ctx = await b.newContext({ baseURL: BASE })
const page = await ctx.newPage()

await page.goto('/login')
await page.waitForTimeout(500)
const loginOk = await page.evaluate(async (u) => {
  const r = await fetch('/api/v1/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ department_id: u.deptId, username: u.username, password: u.password }),
  })
  return r.ok
}, USER)
console.log('登录:', loginOk)

// 进智能助手
await page.goto('/qa')
await page.waitForTimeout(1500)

// 打开文件浮窗
await page.locator('.toolbar-btn:has-text("文件")').click()
await page.waitForTimeout(800)

// 上传文件 A（无会话 → 自动建会话路径）
const fc = page.waitForEvent('filechooser', { timeout: 10_000 })
await page.locator('.btn-ghost:has-text("上传")').click()
const chooser = await fc
await chooser.setFiles(fA)
console.log('已上传文件 A')
await page.waitForTimeout(3000)

// 断言 1：文件区立即显示 A（本次修复点）
let listText = await page.locator('.float-panel').textContent().catch(() => '')
const hasA = listText.includes('文件A')
console.log('上传 A 后文件区立即显示 A:', hasA)

// 上传文件 B
const fc2 = page.waitForEvent('filechooser', { timeout: 10_000 })
await page.locator('.btn-ghost:has-text("上传")').click()
const chooser2 = await fc2
await chooser2.setFiles(fB)
console.log('已上传文件 B')
await page.waitForTimeout(3000)

// 断言 2：A + B 都显示
listText = await page.locator('.float-panel').textContent().catch(() => '')
console.log('上传 B 后文件区显示 A 和 B:', listText.includes('文件A') && listText.includes('文件B'))
console.log('文件区内容:', listText.replace(/\s+/g, ' ').slice(0, 120))

// 清理：删除会话（连带文件）
await page.evaluate(async () => {
  const rl = await fetch('/api/v1/chat/sessions', { credentials: 'include' })
  const list = await rl.json()
  for (const s of list) await fetch('/api/v1/chat/sessions/' + s.id, { method: 'DELETE', credentials: 'include' })
})
await b.close()
console.log('清理完成')
