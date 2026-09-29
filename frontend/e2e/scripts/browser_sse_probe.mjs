// 浏览器网络层对照实验：空白页纯 fetch SSE（无 React），验证浏览器连接是否静默
import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5174'
const CHAOS = { deptId: 'chaos', username: 'chaos01', password: process.env.E2E_CHAOS_PASSWORD ?? '' }
const b = await chromium.launch({ args: ['--no-sandbox'] })
const ctx = await b.newContext({ baseURL: BASE })
const page = await ctx.newPage()
await page.goto('/login')

const loginOk = await page.evaluate(async (c) => {
  const r = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ department_id: c.deptId, username: c.username, password: c.password }) })
  return r.ok
}, CHAOS)
console.log('login:', loginOk)

await page.evaluate(async () => {
  const rl = await fetch('/api/v1/chat/sessions', { credentials: 'include' })
  const body = await rl.text()
  let list = []
  try { list = JSON.parse(body) } catch {}
  if (Array.isArray(list)) {
    for (const s of list) await fetch('/api/v1/chat/sessions/' + s.id, { method: 'DELETE', credentials: 'include' })
  }
})

const sid = await page.evaluate(async () => {
  const r = await fetch('/api/v1/chat/sessions', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: 'probe' }) })
  return (await r.json()).id
})
console.log('session:', sid.slice(0, 8))

const ok = await page.evaluate(async (sid) => {
  window.__probe = []
  const log = (m) => { window.__probe.push(m) }
  const r = await fetch('/api/v1/chat/ask', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sid, question: '你好', mode: 'quick' }) })
  const reader = r.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) { log('EOF'); break }
    buf += dec.decode(value, { stream: true })
    const frames = buf.split('\n\n'); buf = frames.pop() ?? ''
    for (const f of frames) {
      const ev = (f.match(/^event: (.+)$/m) || [])[1] || 'msg'
      const dd = (f.match(/^data: (.+)$/m) || [])[1] || ''
      try { const d = JSON.parse(dd); log('ev=' + ev + ' seq=' + (d.seq ?? '-')) } catch { log('ev=' + ev + ' raw') }
    }
  }
  return 'reader-done'
}, sid)
const lines = await page.evaluate(() => (window.__probe || []).slice(0, 30).join('\n'))
console.log('reader:', ok)
console.log(lines)
await b.close()
