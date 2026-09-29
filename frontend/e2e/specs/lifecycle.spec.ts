// @lifecycle 盲区 B：生命周期
// B1 登出再登录：会话列表/消息恢复（logout 吊销 token 但后端会话保留）
// B2 删除当前会话 → 空态 → 直接发送 → 自动建会话（qaStore:220 路径）
// B3 只读会话（DB 直改 is_readonly）：发送/上传禁用 + placeholder 变更 + 后端 403 E008
// B4 token 吊销（DB 改 token_version，与 logout 同路径）：下一操作 401 → 跳登录
import { test, expect } from '@playwright/test'
import { execSync } from 'child_process'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { waitStreamEnd } from '../helpers/wait'

// SQL 经 stdin 传入（避免 shell 嵌套引号冲突——SQL 含单引号如 'chaos01'）；
// python -c 内必须用真实换行（分号拼接会把函数体压成一行导致 SyntaxError）
const DB_PY = (sql: string) =>
  execSync(
    `cd /opt/tardis/backend && source scripts/env_aip.sh && echo "${sql}" | python -c "`
      + `import sys,asyncio\n`
      + `from app.core.database import get_global_engine\n`
      + `from sqlalchemy import text\n`
      + `sql=sys.stdin.read().strip()\n`
      + `async def m():\n`
      + `    e=get_global_engine()\n`
      + `    async with e.begin() as c: await c.execute(text(sql))\n`
      + `    await e.dispose()\n`
      + `asyncio.run(m())"`,
    { shell: '/bin/bash' },
  )

test.setTimeout(240_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@lifecycle A 登出再登录：会话/消息恢复', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  const MARK = '登出重登恢复测试'
  await page.locator('.chat-input').fill(MARK)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  // 登出（sidebar 退出登录）
  await page.locator('.sidebar-item', { hasText: '退出登录' }).click().catch(() => {})
  await page.waitForURL('**/login', { timeout: 15_000 }).catch(() => {})
  console.log('[lifeA] 已登出')

  // 重新登录
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(1500)

  const text = await page.locator('body').innerText()
  const sessionKept = text.includes(MARK)
  console.log(`[lifeA] 重登后消息保留=${sessionKept}`)
  expect(sessionKept, '[lifeA] 登出再登录会话消息应恢复（后端未删会话）').toBe(true)

  await cleanup(page)
})

test('@lifecycle B 删除当前会话 → 直接发送自动建', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 建会话（发一条）
  await page.locator('.chat-input').fill('你好')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  // 删除当前会话
  const del = page.locator('.session-item.active').first().locator('button, [title*="删除"], .session-del')
  if (await del.count()) await del.first().click()
  await page.waitForTimeout(1000)
  // Popconfirm OK（英文 OK/Cancel，项目未配 zhCN）
  const okBtn = page.locator('.ant-popover:visible .ant-btn-primary')
  if (await okBtn.count()) await okBtn.click()
  await page.waitForTimeout(1500)

  // 空态 → 直接发送（不点新建）→ 应自动建会话并收到回复
  await page.locator('.chat-input').fill('你好')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  const sessions = await (await (apiClient(page)).get('/api/v1/chat/sessions')).json()
  const autoCreated = sessions.filter((s: any) => s.messages_count !== undefined || true).length >= 1
  const gotReply = (await page.locator('.msg-bubble').count()) >= 2
  console.log(`[lifeB] 发送后气泡=${await page.locator('.msg-bubble').count()} 会话数=${sessions.length} 自动建=${autoCreated}`)
  expect(gotReply, '[lifeB] 删除会话后直接发送应自动建会话并收到回复').toBe(true)

  await cleanup(page)
})

// 2026-09-17：删除「@lifecycle C 只读会话：操作禁用 + 后端拦截」——
// 会话只读机制 2026-08-24 已废除（同步后不再冻结会话，前端不再禁用输入框），
// 后端 ask 路径也不再按 is_readonly 拒绝（E008 只剩错误码定义），用例前提整体消失。
// 若日后要恢复该机制，应连同实现一起把本用例补回。

test('@lifecycle D token 吊销：下一操作 401 跳登录', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // DB 改 token_version（模拟过期/吊销——与 logout 同路径 middleware.py:50-59）
  DB_PY(`UPDATE users SET token_version=token_version+1 WHERE username='chaos01'`)

  // 触发 axios 路径的 401（SSE /chat/ask 不走 axios 拦截器——2026-08-12 实测，即审查发现 25：
  // SSE 401 不跳登录，仅在气泡提示"请求失败 (401)"。此处用会话列表（axios GET）触发）
  await page.reload()
  await page.waitForTimeout(2000)
  const toLogin = await page.waitForURL('**/login', { timeout: 15_000 }).then(() => true).catch(() => false)
  console.log(`[lifeD] token 吊销后 reload（axios 路径）跳登录=${toLogin}`)
  expect(toLogin, '[lifeD] token 吊销后 axios 请求应 401 并跳回登录页').toBe(true)

  // 重登（恢复可用，token_version 已 +1 不影响新签发）
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  console.log('[lifeD] 重登正常')

  await cleanup(page)
})
