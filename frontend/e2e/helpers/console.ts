// console error / pageerror / 请求失败收集器（不变量检查核心；故意 abort 的请求不记录）
import type { Page } from '@playwright/test'

export interface CollectedError {
  type: 'console' | 'pageerror' | 'requestfailed'
  text: string
}

export function collectErrors(page: Page) {
  const errors: CollectedError[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      // 2026-09-18：带上来源 URL——浏览器对失败请求的 console 文案只有
      // "Failed to load resource: ... 404 (Not Found)"，**不含是哪个资源**，
      // fuzz 的 I2 不变量就卡在这（报了两个 404 却无法定位）。URL 在 location() 里。
      const loc = msg.location?.()
      errors.push({ type: 'console', text: loc?.url ? `${msg.text()} @ ${loc.url}` : msg.text() })
    }
  })
  page.on('pageerror', (err) => errors.push({ type: 'pageerror', text: String(err) }))
  page.on('requestfailed', (req) => {
    const reason = req.failure()?.errorText
    // 故意 abort 的 SSE 流（停止/切换会话）是预期行为，不算错误
    if (reason && reason !== 'net::ERR_ABORTED') {
      errors.push({ type: 'requestfailed', text: `${req.method()} ${req.url()} -> ${reason}` })
    }
  })
  return {
    errors,
    reset: () => (errors.length = 0),
    count: () => errors.length,
    describe: () => errors.map((e) => `[${e.type}] ${e.text}`).join('\n'),
  }
}
