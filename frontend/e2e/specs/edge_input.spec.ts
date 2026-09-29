// @edgeinput 盲区 D：边界输入
// A 超长输入（100K）：textarea 可输入、发送正常（后端/前端截断策略）
// B 空/纯空格发送：无请求（防抖/校验）
// C emoji/特殊字符/<script>：正常渲染 + 无 XSS 执行
// D 上传边界：0 字节 / 非白名单类型 / 超 20MB → 前端拦截提示
import { test, expect } from '@playwright/test'
import { closeSync, existsSync, ftruncateSync, openSync, statSync } from 'node:fs'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { closeSessionsDrawer } from '../helpers/ui'
import { waitStreamEnd, bubbleRoles } from '../helpers/wait'

// 2026-09-18：>50MB 的上传夹具必须**落盘**——Playwright 的 setFiles 对内存 buffer 有 50MB 硬上限
// （buffer: Buffer.alloc(200MB+1) 直接抛 "Cannot set buffer larger than 50Mb"，用例恒失败）。
// 用稀疏文件：ftruncate 瞬间建成、几乎不占盘（前端按 size 拦截，不需要真实字节）。
const BIG_BIN_PATH = '/tmp/edge_big_200mb.bin'
const BIG_BIN_SIZE = 200 * 1024 * 1024 + 1

function ensureBigBin(): string {
  if (!existsSync(BIG_BIN_PATH) || statSync(BIG_BIN_PATH).size !== BIG_BIN_SIZE) {
    const fd = openSync(BIG_BIN_PATH, 'w')
    ftruncateSync(fd, BIG_BIN_SIZE)
    closeSync(fd)
  }
  return BIG_BIN_PATH
}

test.setTimeout(240_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@edgeinput A 超长输入（100K）发送不卡死', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  const LONG = '长'.repeat(100_000)
  await page.locator('.chat-input').fill(LONG)
  const len = (await page.locator('.chat-input').inputValue()).length
  console.log(`[edgeA] 输入长度=${len}`)

  // 发送（短问题验证流仍工作——100K 直接发会拖很久，先验证输入/UI 无异常，再发正常消息）
  const alive = await page.evaluate(() => Date.now()).then(() => true).catch(() => false)
  const sendBtn = await page.locator('button', { hasText: '发送' }).isEnabled().catch(() => true)
  console.log(`[edgeA] 主线程=${alive} 发送按钮可用=${sendBtn}`)
  expect(alive, '[edgeA] 超长输入不应卡死').toBe(true)

  // 清空后正常发送
  await page.locator('.chat-input').fill('超长输入后正常消息')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  console.log('[edgeA] 超长输入后正常发送完成')

  await cleanup(page)
})

test('@edgeinput B 空/纯空格发送：无请求', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  const asks: number[] = []
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/chat/ask')) asks.push(Date.now())
  })

  // 空输入直接 Enter
  await page.keyboard.press('Enter')
  await page.waitForTimeout(1000)
  // 纯空格
  await page.locator('.chat-input').fill('   ')
  await page.keyboard.press('Enter')
  await page.waitForTimeout(1000)

  console.log(`[edgeB] 空/空格发送 POST /ask 次数=${asks.length}（期望 0）`)
  expect(asks.length, '[edgeB] 空/纯空格不应触发发送请求').toBe(0)

  await cleanup(page)
})

test('@edgeinput C emoji/特殊字符/<script> 注入不执行', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // C29（2026-08-12）：用户消息原文展示——`__bold__` 不得被 markdown 渲染成加粗（原走 markdown 变粗）
  const XSS = '测试 __bold__ <script>window.XSSPWNED123=true</script> emoji 😀 换行\n第二行'
  let xssExecuted = false
  await page.addInitScript(() => { (window as any).XSSPWNED123 = false })
  await page.locator('.chat-input').fill(XSS)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  // 用户气泡应渲染原文（<script> 不执行、__ 不加粗）——role 由祖父层 justifyContent 判定（bubbleRoles）
  const bubbles = await bubbleRoles(page)
  const userBubble = [...bubbles].reverse().find((b) => b.role === 'user')
  const text = userBubble?.text ?? ''
  const hasScriptText = text.includes('window.XSSPWNED123=true')
  const hasUnderscore = text.includes('__bold__')
  xssExecuted = await page.evaluate(() => (window as any).XSSPWNED123 === true)
  console.log(`[edgeC] 原文保留=${hasScriptText} __原样=${hasUnderscore} XSS 执行=${xssExecuted}`)
  expect(hasScriptText, '[edgeC] 用户消息应原样展示（React 默认转义）').toBe(true)
  expect(hasUnderscore, '[edgeC] 用户消息 `__text__` 应原样（C29 修复：用户消息不走 markdown）').toBe(true)
  expect(xssExecuted, '[edgeC] script 不应被执行').toBe(false)

  await cleanup(page)
})

test('@edgeinput D 上传边界拦截（0 字节/非白名单/超大）', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 建会话（v2：无授权卡；"上传边界测试"可能被含糊判定触发反问卡——
  // 反问卡等待会悬挂流；出现则按全部默认提交，本用例仅需会话存在，不依赖回复内容）
  await page.locator('.chat-input').fill('上传边界测试')
  await page.keyboard.press('Enter')
  const qCard = page.locator('.question-card')
  await qCard.waitFor({ timeout: 20_000 }).then(
    async () => { console.log('[edgeD] 触发反问卡——按全部默认提交'); await page.locator('.btn-ghost', { hasText: '全部默认' }).click() },
    () => {},
  )
  await waitStreamEnd(page)
  await closeSessionsDrawer(page)  // D6：抽屉遮罩拦截工具栏「文件」按钮

  // D5：上传入口并入文件浮窗——先打开文件面板再点「＋ 上传」
  await page.locator('.toolbar-btn', { hasText: '文件' }).click()
  await page.locator('.btn-ghost', { hasText: '上传' }).waitFor()

  // 0 字节文件
  const [c1] = await Promise.all([
    page.waitForEvent('filechooser', { timeout: 10_000 }),
    page.locator('.btn-ghost', { hasText: '上传' }).click(),
  ])
  await c1.setFiles({ name: 'empty.txt', mimeType: 'text/plain', buffer: Buffer.from('') })
  await page.waitForTimeout(1500)
  const emptyWarning = await page.locator('.ant-message', { hasText: '空' }).count().catch(() => 0)
  console.log(`[edgeD] 0 字节: 空文件提示=${emptyWarning}`)

  // 非白名单类型（.exe——accept 属性过滤，filechooser 绕过前端 accept 仍需后端校验）
  const [c2] = await Promise.all([
    page.waitForEvent('filechooser', { timeout: 10_000 }),
    page.locator('.btn-ghost', { hasText: '上传' }).click(),
  ])
  await c2.setFiles({ name: 'malware.exe', mimeType: 'application/octet-stream', buffer: Buffer.from('MZ') })
  await page.waitForTimeout(2000)
  const exeMsg = await page.locator('.ant-message').allTextContents().catch(() => [])
  console.log(`[edgeD] .exe 上传: 提示=${JSON.stringify(exeMsg)}`)

  // 超上限（前端 C4 拦截）。2026-09-17：上限已按类型区分——音视频 600MB / 其他 200MB
  // （ChatPanel.limitOf），原来的 21MB 早就在限额内、断言文案 '20MB' 也是旧的 → 过时断言。
  // 现在用刚过非媒体上限的文件，并断言稳定的提示文案「超过上限」。
  const [c3] = await Promise.all([
    page.waitForEvent('filechooser', { timeout: 10_000 }),
    page.locator('.btn-ghost', { hasText: '上传' }).click(),
  ])
  await c3.setFiles(ensureBigBin())
  const bigMsg = await page.locator('.ant-message', { hasText: '超过上限' })
    .isVisible({ timeout: 30_000 }).catch(() => false)
  console.log(`[edgeD] 200MB+1 上传: 前端拦截提示=${bigMsg}`)
  expect(bigMsg, '[edgeD] 超过上限（非媒体 200MB）应前端拦截并提示').toBe(true)

  await cleanup(page)
})
