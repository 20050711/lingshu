import type { Page } from '@playwright/test'

// D6（2026-08-14）：会话列浮层抽屉化——specs 旧写法 qa_sessions_open=1 会让抽屉带
// 全屏遮罩常开（遮罩拦截主区点击）。需要操作主区前先点遮罩关闭抽屉。
export async function closeSessionsDrawer(page: Page) {
  const mask = page.locator('.sessions-mask')
  if (await mask.count()) {
    await mask.click({ position: { x: 500, y: 300 } }).catch(() => {})
    await mask.waitFor({ state: 'detached', timeout: 3000 }).catch(() => {})
  }
}

// 2026-09-18：抽屉的**打开**入口是工具栏「会话列表」按钮（ChatPanel `onOpenPanel('sessions')`）。
// 需要它的原因：会话项 onClick 是 `switchSession(id) + toggleSessions()`——**切一次会话就自动收起抽屉**，
// 所以「切走再切回」这类连续操作每一步前都必须重开，否则 `.session-item` 停在 translateX(-100%)
// 的屏外位置，点击会被左侧栏拦截（报 sidebar-item intercepts pointer events）。
export async function openSessionsDrawer(page: Page) {
  const mask = page.locator('.sessions-mask')
  if (await mask.count()) return           // 已经开着
  await page.locator('.toolbar-btn', { hasText: '会话列表' }).click()
  await mask.waitFor({ state: 'attached', timeout: 5000 }).catch(() => {})
}
