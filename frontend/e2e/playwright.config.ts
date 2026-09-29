// 乱操作 E2E（Playwright）——双入口：部署形态 :24426 / dev :5174
// 硬约束：workers=1（后端单 worker：confirm 内存态 asyncio.Future + 防爆破同 IP 计数）
import { defineConfig } from '@playwright/test'

const shared = {
  screenshot: 'only-on-failure',
  video: 'retain-on-failure',
  trace: 'retain-on-failure',
  actionTimeout: 15_000,
  // 浏览器：WSL 内 Chrome for Testing 151（手动装至 ~/.cache/ms-playwright/chromium-1234/chrome-linux/）
  // 2026-08-19（S1-2）：禁用后台节流——waitFor 期间页面无 JS 活动会被 chromium 节流
  // （fetch SSE 流不消费 → TCP 缓冲满 → 服务端 tail_stream send 阻塞 → 长连接静默，
  // 曾致 agent_v2 复杂任务修订重提收不到 plan 事件；真实用户前台使用无此问题）
  launchOptions: {
    args: ['--no-sandbox', '--disable-background-timer-throttling',
      '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding'],
  },
}

export default defineConfig({
  testDir: './specs',
  // 全局 120s；长场景（confirm 180s 上限/fuzz）在 spec 内 test.setTimeout 覆盖
  timeout: 120_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0, // 真 LLM 场景失败=真实信号，不自动重试
  reporter: [
    ['line'],
    ['html', { outputFolder: '../../e2e-report/html', open: 'never' }],
  ],
  outputDir: '../../e2e-report/artifacts',
  projects: [
    { name: 'deploy', use: { baseURL: 'http://127.0.0.1:24426', ...shared } },
    { name: 'dev', use: { baseURL: 'http://127.0.0.1:5174', ...shared } },
  ],
})
