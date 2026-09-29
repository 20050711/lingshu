// @toolproc 用户报告（2026-09-17）：工具调用的过程（气泡上方的时间线：预告/工具/小结）
// 在**刷新页面**后、或**切到别的会话再切回来**后会消失。
//
// 复现：发一条必用工具的问题 → 等流式结束 → 数时间线行 → reload → 再数 → 断言一致。
// 依赖：后端 LLM_MOCK=1（模拟序列 = intent_event + file_search，零 LLM 费用）；
//      非 mock 后端亦可跑（真实 LLM 会用工具，只是耗时/费用由档位决定）。
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { waitStreamEnd } from '../helpers/wait'
import { collectErrors } from '../helpers/console'

test.setTimeout(240_000)

const QUESTION = '用 Python 计算 1 到 100 的和'

async function timelineCount(page: import('@playwright/test').Page): Promise<number> {
  const tool = await page.getByTestId('tl-tool').count()
  const intent = await page.getByTestId('tl-intent').count()
  const result = await page.getByTestId('tl-result').count()
  return tool + intent + result
}

test('@toolproc 工具调用过程：刷新后仍在', async ({ page }) => {
  // 会话列默认收起（D6 抽屉化）——切换会话前需打开，否则列表被左侧导航遮挡
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  const errs = collectErrors(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 新建会话（避免复用历史会话干扰计数）
  await page.locator('.toolbar-btn', { hasText: '+ 新建' }).click().catch(() => {})
  await page.waitForTimeout(500)

  await page.locator('.chat-input').fill(QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  const before = await timelineCount(page)
  console.log(`[toolproc] 发送后时间线行数=${before}`)
  expect(before, '发送完成后应能看到工具调用过程（时间线）').toBeGreaterThan(0)

  // 刷新页面 → 恢复会话
  await page.reload()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(3500) // 等待 restore（含 3s 空重试窗口）
  const after = await timelineCount(page)
  console.log(`[toolproc] 刷新后时间线行数=${after}`)
  expect(after, '刷新后工具调用过程应保留').toBe(before)

  // 切到别的会话再切回（点会话项会关闭抽屉 → 切回前按需重开；
  // 抽屉收起时是 translateX(-100%)——DOM 仍在，故用遮罩 .sessions-mask 判断开合）
  const openSessions = async () => {
    if ((await page.locator('.sessions-mask').count()) === 0) {
      await page.locator('.toolbar-btn', { hasText: '会话列表' }).click()
      await page.locator('.sessions-mask').waitFor({ timeout: 5000 })
    }
  }
  // 用当前会话标题定位（会话列按最近活动排序，切走后原会话不一定还在原位——
  // 早先按 `:not(.active).first()` 点回，deploy 全量跑时点到了第三个会话 → 行数 0 的假失败）
  await openSessions()
  // 会话行文本含标题 + 模式/计费徽标 + 「删除」按钮 → 取标题首段做子串匹配（原整串精确匹配
  // 在 deploy 全量跑时匹配不到：innerText 带换行与按钮文字）
  const activeTitle = (await page.locator('.session-item.active').first().innerText()).trim().split('\n')[0].trim().slice(0, 12)
  const others = page.locator('.session-item:not(.active)')
  if (activeTitle && (await others.count()) >= 1) {
    await others.first().click()
    await page.waitForTimeout(1200)
    await openSessions()
    await page.locator('.session-item', { hasText: activeTitle }).first().click()
    await page.waitForTimeout(1500)
    const back = await timelineCount(page)
    console.log(`[toolproc] 切走再切回后时间线行数=${back}`)
    expect(back, '切会话再切回后工具调用过程应保留').toBe(before)
  } else {
    console.log('[toolproc] 仅 1 个会话，跳过切会话分支')
  }

  expect(errs.count(), `控制台/请求错误：\n${errs.describe()}`).toBe(0)
})

// 运行中那一轮：刷新后续播必须**整轮回放**
// （2026-09-17 修复点：resumeSession 曾沿断点 seq 只回放增量 → 刷新前已发生的工具过程永久缺失）
test('@toolproc 运行中刷新：续播后工具过程仍在', async ({ page }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  await page.locator('.toolbar-btn', { hasText: '+ 新建' }).click().catch(() => {})
  await page.waitForTimeout(500)

  await page.locator('.chat-input').fill(QUESTION)
  await page.keyboard.press('Enter')
  // 等本轮**已出现工具行**再刷新（否则断言退化为"整轮都在刷新之后"，证明不了回放）
  await page.locator('.btn-ghost', { hasText: '停止' }).waitFor({ timeout: 30_000 })
  await page.getByTestId('tl-tool').first().waitFor({ timeout: 30_000 })
  const mid = await timelineCount(page)
  console.log(`[toolproc] 刷新前时间线行数=${mid}`)

  const askCalls: string[] = []
  page.on('request', (r) => {
    if (r.url().includes('/chat/ask')) askCalls.push(`${Date.now() % 100000} ${JSON.stringify(r.postDataJSON?.() ?? '')}`)
  })
  await page.reload()
  await page.locator('.chat-input').waitFor()
  // 续播（整轮回放）→ 等本轮结束
  await page.waitForTimeout(4000)
  const afterReload = await timelineCount(page)
  console.log(`[toolproc] 运行中刷新后续播时间线行数=${afterReload}`)
  const dump = await page.evaluate(() => Array.from(document.querySelectorAll('.msg-bubble')).map((b) => {
    // 时间线是气泡的**前一个兄弟**节点（MessageItem 结构：<div maxWidth><TimelineList/><div class=msg-bubble>）
    const tl = b.previousElementSibling
    const rows = tl ? Array.from(tl.querySelectorAll('[data-testid]')).map((r) => r.getAttribute('data-testid')) : []
    return { text: (b.textContent || '').slice(0, 16), rows }
  }))
  console.log('[toolproc] DOM dump=' + JSON.stringify(dump))
  console.log('[toolproc] 刷新后 /chat/ask 请求=' + JSON.stringify(askCalls))
  await page.screenshot({ path: '/tmp/toolproc_midrun.png', fullPage: false })
  expect(afterReload, '运行中刷新后，刷新前已发生的工具过程应回放出来').toBeGreaterThanOrEqual(Math.max(mid, 1))
})
