// 升级轮长连接 probe：反问→plan rev=1→reject→观察 rev=2 与 heartbeat（raw fetch，无 React）
import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5174'
const CHAOS = { deptId: 'chaos', username: 'chaos01', password: process.env.E2E_CHAOS_PASSWORD ?? '' }
const b = await chromium.launch({ args: ['--no-sandbox'] })
const ctx = await b.newContext({ baseURL: BASE })
const page = await ctx.newPage()
await page.goto('/login')
await page.evaluate(async (c) => {
  await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ department_id: c.deptId, username: c.username, password: c.password }) })
}, CHAOS)
await page.evaluate(async () => {
  const rl = await fetch('/api/v1/chat/sessions', { credentials: 'include' })
  let list = []
  try { list = await rl.json() } catch {}
  if (Array.isArray(list)) for (const s of list) await fetch('/api/v1/chat/sessions/' + s.id, { method: 'DELETE', credentials: 'include' })
})
const sid = await page.evaluate(async () => (await (await fetch('/api/v1/chat/sessions', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_id: 'p2' }) })).json()).id)
console.log('session:', sid.slice(0, 8))

// 快速轮（同步等 done）
await page.evaluate(async (sid) => {
  const r = await fetch('/api/v1/chat/ask', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sid, question: '你好', mode: 'quick' }) })
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const frames = buf.split('\n\n'); buf = frames.pop() ?? ''
    for (const f of frames) if (f.includes('"done"') || f.startsWith('event: done')) { reader.cancel(); return 'r1-done' }
  }
  return 'r1-eof'
}, sid)
console.log('r1 done')

// 升级轮：流式读取 + 事件循环处理（页面内异步循环：反问自动答、plan reject）
const result = await page.evaluate(async (sid) => {
  const out = []
  const t0 = Date.now()
  const log = (m) => out.push('+' + Math.round((Date.now() - t0) / 1000) + 's ' + m)
  const answered = new Set()
  let rejected = false
  const r = await fetch('/api/v1/chat/ask', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sid, question: '', upgrade: true, mode: 'complex' }) })
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = ''
  const deadline = Date.now() + 420000
  while (Date.now() < deadline) {
    const pr = reader.read()
    const timer = new Promise((res) => setTimeout(() => res('timeout'), 20000))
    const { done, value } = await Promise.race([pr, timer])
    if (value === 'timeout') { log('20s 无数据（timeout）'); continue }
    if (done) { log('EOF'); break }
    buf += dec.decode(value, { stream: true })
    const frames = buf.split('\n\n'); buf = frames.pop() ?? ''
    for (const f of frames) {
      const ev = (f.match(/^event: (.+)$/m) || [])[1] || 'msg'
      const dd = (f.match(/^data: (.+)$/m) || [])[1] || ''
      let d = {}
      try { d = JSON.parse(dd) } catch {}
      if (ev === 'heartbeat') { log('heartbeat'); continue }
      if (ev === 'question' && !answered.has(d.question_id)) {
        answered.add(d.question_id)
        const ans = (d.questions || []).map((q, i) => ({ question_idx: i, selected: (q.recommended || [0]).slice(0, 1) }))
        await fetch('/api/v1/chat/answer', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sid, question_id: d.question_id, answers: ans }) })
        log('answer q=' + d.question_id.slice(0, 8))
      } else if (ev === 'plan' && !rejected) {
        rejected = true
        log('plan rev=' + (d.revision ?? 1) + ' → reject')
        await fetch('/api/v1/chat/plan-approve', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sid, plan_id: d.plan_id, decision: 'reject', feedback: '步骤精简，合并同类操作' }) })
      } else if (ev === 'plan' && rejected) {
        log('plan rev=' + (d.revision ?? 1) + ' 修订重提 ✓')
        return out.join('\n')
      } else if (ev === 'done' || ev === 'error' || ev === 'aborted') {
        log('terminal ' + ev)
        return out.join('\n')
      }
    }
  }
  out.push('TIMEOUT 420s')
  return out.join('\n')
}, sid)
console.log(result)
await b.close()
