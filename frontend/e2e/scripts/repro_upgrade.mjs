#!/usr/bin/env node
// repro_upgrade.mjs — 复杂任务「升级→反问→批准卡」链路取证（2026-08-19 S1-2）
//
// 背景：agent_v2.spec.ts 复杂任务曾 300s 超时——升级轮 agent 反问后无 /chat/answer
// 到达、无后续 LLM 调用。本脚本：
//   1) 弹性处理反问（不限 3 轮——排除测试轮询 break 干扰）
//   2) 全链路采样：反问卡可见性 / 网络请求（ask/answer/plan-approve）/ localStorage
//      qa_seq 断点 / 前端 console 与 pageerror
//   3) 输出时间线 JSON 到 stdout，供与后端日志（/tmp/uvicorn.log grep 取证）对照
//
// 用法: node scripts/repro_upgrade.mjs [--base http://127.0.0.1:5174] [--budget-ms 480000]
// 跑完同跑取证:
//   grep -E "ask_user|question|plan-approve|chat/answer|409|后台任务" /tmp/uvicorn.log | tail -40
//   redis-cli -a $REDIS_PASSWORD LRANGE "sse_events:{sid}" 0 -1
import { chromium } from 'playwright'

const BASE = process.argv[2] || 'http://127.0.0.1:5174'
const BUDGET_MS = Number(process.argv[3] || 480_000)
const CHAOS = { deptId: 'chaos', deptName: '随机操作测试', username: 'chaos01', password: process.env.E2E_CHAOS_PASSWORD ?? '' }

const t0 = Date.now()
const ts = () => `+${((Date.now() - t0) / 1000).toFixed(1)}s`
const log = (type, detail) => console.log(JSON.stringify({ t: ts(), type, ...(typeof detail === 'object' ? detail : { detail }) }))

async function main() {
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const ctx = await browser.newContext({ baseURL: BASE, viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()
  const sessionIds = []

  // 网络/控制台取证
  page.on('console', (m) => {
    const text = m.text()
    if (/(error|warn|seq|question|plan)/i.test(text)) log('console', { level: m.type(), text: text.slice(0, 200) })
  })
  page.on('pageerror', (e) => log('pageerror', String(e).slice(0, 300)))
  page.on('request', (r) => {
    if (/\/chat\/(ask|answer|plan-approve|stop)/.test(r.url())) {
      log('req', { url: r.url().replace(BASE, ''), method: r.method() })
    }
  })
  page.on('response', async (r) => {
    if (/\/chat\/(ask|answer|plan-approve|stop)/.test(r.url())) {
      log('resp', { url: r.url().replace(BASE, ''), status: r.status() })
    }
  })

  // 1. 登录（UI 三要素）
  await page.goto('/login')
  await page.locator('.login-card').waitFor()
  await page.waitForResponse((r) => r.url().includes('/auth/departments'), { timeout: 15_000 }).catch(() => {})
  await page.locator('.login-card .ant-select').click()
  await page.locator('.ant-select-item-option', { hasText: CHAOS.deptName }).first().click({ timeout: 10_000 })
  await page.locator('.login-input').nth(0).fill(CHAOS.username)
  await page.locator('.login-input').nth(1).fill(CHAOS.password)
  await page.locator('.login-btn').click()
  await page.waitForURL('**/home', { timeout: 15_000 })
  log('auth', '登录成功')

  // 2. 清理旧会话（隔离环境干扰——避免遗留 running 任务 409）
  const cleanup = await page.evaluate(async () => {
    const res = await fetch('/api/v1/chat/sessions', { credentials: 'include' })
    if (!res.ok) return { ok: false, status: res.status }
    const list = await res.json()
    for (const s of list) await fetch(`/api/v1/chat/sessions/${s.id}`, { method: 'DELETE', credentials: 'include' }).catch(() => {})
    return { ok: true, deleted: list.length }
  })
  log('cleanup', cleanup)

  // 3. 进入 QA 页
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 4. 快速轮（建立升级前置历史）
  await page.locator('.chat-input').fill('你好')
  await page.keyboard.press('Enter')
  await page.locator('.btn-ghost', { hasText: '停止' }).waitFor({ timeout: 60_000 }).catch(() => {})
  // 等流结束（停止按钮消失 + 最后气泡非思考中）
  await page.waitForFunction(() => {
    const stop = [...document.querySelectorAll('.btn-ghost')].some((b) => b.textContent.includes('停止'))
    if (stop) return false
    const bubbles = [...document.querySelectorAll('.msg-bubble')]
    const last = bubbles[bubbles.length - 1]
    return !last || !(last.textContent.includes('思考中') || last.textContent.includes('正在处理'))
  }, { timeout: 150_000 }).catch(() => log('warn', '快速轮未在预算内结束'))
  log('round1', '快速轮完成')

  // 5. 升级复杂任务（关 D6 抽屉遮罩）
  const mask = page.locator('.sessions-mask')
  if (await mask.count()) await mask.click({ position: { x: 500, y: 300 } }).catch(() => {})
  await page.locator('.mode-btn', { hasText: '复杂任务' }).click()
  await page.locator('.ant-modal-confirm', { hasText: '升级为复杂任务模式' }).waitFor({ timeout: 15_000 })
  await page.locator('.ant-modal-confirm button', { hasText: /升\s*级/ }).click()
  await page.waitForFunction(() => {
    const btn = [...document.querySelectorAll('.mode-btn')].find((b) => b.textContent.includes('复杂任务'))
    return btn && btn.className.includes('active')
  }, { timeout: 15_000 }).catch(() => log('warn', '复杂模式高亮未确认'))
  log('upgrade', '升级完成，复杂模式')

  // 6. 弹性反问处理 + 批准卡等待（全局预算）
  const qCard = page.locator('.question-card')
  const planModal = page.locator('.ant-modal', { hasText: '计划批准' })
  let qCount = 0
  let firstQuestionSeq = null
  const deadline = Date.now() + BUDGET_MS
  while (Date.now() < deadline) {
    // 采样 localStorage qa_seq（找当前会话 id）
    const seqInfo = await page.evaluate(() => {
      const keys = Object.keys(localStorage).filter((k) => k.startsWith('qa_seq:')).map((k) => [k, localStorage.getItem(k)])
      return keys
    })
    if (seqInfo.length && !sessionIds.length) sessionIds.push(...seqInfo.map(([k]) => k.replace('qa_seq:', '')))
    if (await planModal.isVisible({ timeout: 500 }).catch(() => false)) {
      log('plan', { waitMs: Date.now() - t0, seqKeys: seqInfo, qCount })
      const title = await planModal.locator('.ant-modal-title').innerText().catch(() => '')
      log('plan', { title })
      break
    }
    const qVisible = await qCard.isVisible({ timeout: 500 }).catch(() => false)
    if (qVisible) {
      qCount++
      if (firstQuestionSeq === null) firstQuestionSeq = seqInfo
      log('question', { round: qCount, seqKeys: seqInfo })
      await page.locator('.btn-ghost', { hasText: '全部默认' }).click()
      await page.waitForTimeout(1500)
    }
    await page.waitForTimeout(500)
  }
  const outcome = await planModal.isVisible().catch(() => false)
  log(outcome ? 'success' : 'timeout', {
    budgetMs: BUDGET_MS, qCount, sessionIds, firstQuestionSeq,
    seqKeysNow: await page.evaluate(() => Object.entries(localStorage).filter(([k]) => k.startsWith('qa_seq:')).map(([k, v]) => [k, v])),
  })

  // 7. 失败时截图（取证）
  if (!outcome) {
    await page.screenshot({ path: '/tmp/repro_upgrade_fail.png', fullPage: false })
    log('screenshot', '/tmp/repro_upgrade_fail.png')
  }
  await browser.close()
  process.exit(outcome ? 0 : 1)
}

main().catch((e) => { console.error('FATAL', e); process.exit(2) })
