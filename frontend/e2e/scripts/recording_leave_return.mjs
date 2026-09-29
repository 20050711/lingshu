// 验证（2026-08-27）：录音中切页（定制化卡片/其他页面）→ 录音不中断 + 时间轴正确 + 停止后上传链路
// 假设备：--use-fake-device-for-media-stream（假麦克风带音频）+ --use-fake-ui（自动授权）
// 说明：Chromium 无法 fake「系统声音捕获」的音频轨（真实声卡），故先取消勾选测单轨麦克风链路；
//       系统声音轨与麦克风轨的 recorder 生命周期完全相同（模块级引用），切页行为一致。
import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:24425'
const USER = { deptId: 'market', username: 'walkthrough', password: 'Py@sqLPELI%kY5ot' }

const b = await chromium.launch({
  args: [
    '--no-sandbox',
    '--use-fake-device-for-media-stream',
    '--use-fake-ui-for-media-stream',
    '--disable-background-timer-throttling',
  ],
})
const ctx = await b.newContext({ baseURL: BASE })
const page = await ctx.newPage()

// 1. API 登录（同 cookie context 供后续页面请求；需先在同一 origin 页面内 fetch）
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

// 2. 进入会议纪要（SPA 导航：/tools 点「会议纪要」卡片；localhost 是 secure context 不触发 https 跳转）
await page.goto('/tools')
await page.waitForTimeout(1200)
await page.locator('.func-card:has-text("会议纪要")').first().click()
await page.waitForTimeout(1500)
// 取消系统声音勾选（fake 环境无真实系统音频轨）
const sysChk = page.locator('label:has-text("录制系统声音") input[type="checkbox"]')
if (await sysChk.isVisible().catch(() => false)) {
  if (await sysChk.isChecked().catch(() => false)) { await sysChk.click(); console.log('已取消系统声音勾选') }
}
const recBtn = page.locator('button:has-text("开始录音")')
console.log('录音按钮可见:', await recBtn.isVisible())

// 3. 开始录音
await recBtn.click()
await page.waitForTimeout(2500)
const readSeconds = async () => {
  const t = await page.locator('span[style*="tabular-nums"]').first().textContent().catch(() => '')
  const m = ((t || '').trim()).match(/(\d+):(\d+)/)
  return m ? Number(m[1]) * 60 + Number(m[2]) : 0
}
const s1 = await readSeconds()
console.log('录音中时间轴(约2.5s后):', s1)

// 4. 切到定制化卡片页（侧边栏 SPA 导航——整页 goto 会刷新销毁录音，必须真实导航点击）
await page.locator('.sidebar-item:has-text("定制化工具")').click()
await page.waitForTimeout(4000)
console.log('已切到 /tools（SPA 组件卸载，录音应继续）')

// 5. 切回会议纪要（卡片 SPA 导航）
await page.locator('.func-card:has-text("会议纪要")').first().click()
await page.waitForTimeout(1500)
const tips = await page.locator('.ant-message-notice').allTextContents().catch(() => [])
console.log('恢复提示:', JSON.stringify(tips))
const s2 = await readSeconds()
console.log('切回后时间轴:', s2)
const expectMin = Math.floor(2.5 + 4 + 1.5) - 1
console.log(`时间轴 ≥ ${expectMin}s（切页期间时间继续走）:`, parseInt(s2) >= expectMin)

// 6. 停止录音 → 应正常上传
await page.locator('button:has-text("停止录音")').click()
await page.waitForTimeout(8000)
const tagText = await page.locator('.ant-tag').first().textContent().catch(() => '')
console.log('停止后状态标签:', tagText)

// 7. 等转写完成（最多 90s）
let final = ''
for (let i = 0; i < 30; i++) {
  await page.waitForTimeout(3000)
  const rows = await page.locator('.ant-list-item').allTextContents().catch(() => [])
  if (rows.length) {
    const first = rows[0]
    if (first.includes('转写完成') || first.includes('失败') || first.includes('转写中')) {
      final = first.replace(/\s+/g, ' ').slice(0, 90)
      if (first.includes('转写完成') || first.includes('失败')) break
    }
  }
}
console.log('历史列表最新:', final || '(仍在处理)')

// 8. 清理：删除本次记录（API）
const del = await page.evaluate(async () => {
  const r = await fetch('/api/v1/tools/meetings', { credentials: 'include' })
  const list = (await r.json()).meetings || []
  let n = 0
  for (const m of list.slice(0, 1)) {
    await fetch('/api/v1/tools/meetings/' + m.meeting_id, { method: 'DELETE', credentials: 'include' })
    n++
  }
  return n
})
console.log('清理记录数:', del)
await b.close()
