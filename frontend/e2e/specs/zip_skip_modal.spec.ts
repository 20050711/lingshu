// @zipskip 智能助手 zip 直传：命中安全黑名单的成员被跳过 → 小浮窗列出清单（2026-09-17 用户要求）
// 依赖后端统一的可执行载荷黑名单（services/zip_utils.EXEC_BLOCKLIST）——跳过的成员不落盘。
import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { loginViaUI } from '../helpers/auth'

test.setTimeout(120_000)

const ZIP_PATH = '/tmp/e2e_zip_skip.zip'

function makeZip() {
  // python 造包（成员：正常 .txt / .exe / .xlsm / macOS 垃圾）
  execFileSync('python3', ['-c', `
import zipfile
z = zipfile.ZipFile('${ZIP_PATH}', 'w')
z.writestr('资料/说明.txt', 'hello')
z.writestr('资料/工具.exe', b'MZ fake')
z.writestr('资料/宏.xlsm', b'fake')
z.writestr('__MACOSX/._说明.txt', b'junk')
z.close()
`])
}

test('@zipskip zip 直传跳过名单：出浮窗 + 可确认关闭', async ({ page }) => {
  makeZip()
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 上传入口在「文件」浮窗里（D5）
  await page.locator('.toolbar-btn', { hasText: '文件' }).click()
  const [fc] = await Promise.all([
    page.waitForEvent('filechooser'),
    page.locator('button', { hasText: '上传' }).first().click(),
  ])
  await fc.setFiles(ZIP_PATH)

  const modal = page.locator('.ant-modal', { hasText: '已跳过' })
  await modal.waitFor({ timeout: 30_000 })
  const text = (await modal.innerText()).replace(/\s+/g, ' ')
  console.log('[zipskip] 浮窗文案=' + text.slice(0, 160))
  expect(text, '浮窗应列出被跳过的可执行文件').toContain('工具.exe')
  expect(text, '浮窗应列出宏文档').toContain('宏.xlsm')
  expect(text, '浮窗应说明未落盘/未导入').toMatch(/没有导入|未导入|不会保存/)

  await page.locator('.ant-modal', { hasText: '已跳过' }).locator('button', { hasText: '知道了' }).click()
  // antd Modal 关闭后仍留在 DOM（隐藏）——按可见性断言
  await expect(page.locator('.ant-modal', { hasText: '已跳过' })).toBeHidden({ timeout: 10_000 })
})
