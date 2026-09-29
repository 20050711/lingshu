// 流式等待与气泡统计（结构性断言：不断言 LLM 具体文本，只断言轮次/条数/按钮态）
import { expect, type Page } from '@playwright/test'
import { TIMEOUT } from './constants'

/** 等「■ 停止」出现 = 流式已开始（发送后 LLM 首包有延迟，必须轮询而非固定 sleep） */
export async function waitStreamingStart(page: Page) {
  await page.locator('.btn-ghost', { hasText: '停止' }).waitFor({ timeout: TIMEOUT.stream })
}

/** 等流式结束：停止按钮消失 + 最后气泡不再是「思考中/正在处理」 */
export async function waitStreamEnd(page: Page, timeoutMs: number = TIMEOUT.stream) {
  await expect(page.locator('.btn-ghost', { hasText: '停止' })).toHaveCount(0, {
    timeout: timeoutMs,
  })
  await expect
    .poll(
      async () => {
        const bubbles = await bubbleRoles(page)
        const last = bubbles[bubbles.length - 1]
        return last ? !(last.text.includes('思考中') || last.text.includes('正在处理')) : true
      },
      { timeout: timeoutMs },
    )
    .toBe(true)
}

/** 气泡统计：role 由祖父层 justifyContent 判定（user=flex-end 蓝色右对齐）。
 * 结构：外层 flex div(:100) > 中间 maxWidth div(:101) > .msg-bubble(:110)——flex 判定在祖父层 */
export async function bubbleRoles(page: Page): Promise<{ role: 'user' | 'assistant'; text: string }[]> {
  return page.locator('.msg-bubble').evaluateAll((els) =>
    els.map((el) => {
      const grand = el.parentElement?.parentElement
      const justify = grand ? grand.style.justifyContent : ''
      return {
        role: justify === 'flex-end' ? 'user' : 'assistant',
        text: (el as HTMLElement).innerText,
      }
    }),
  )
}

/** 统计某角色气泡数 */
export async function countBubbles(page: Page, role: 'user' | 'assistant'): Promise<number> {
  const all = await bubbleRoles(page)
  return all.filter((b) => b.role === role).length
}
