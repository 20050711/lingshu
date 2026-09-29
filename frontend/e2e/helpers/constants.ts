// 乱操作 E2E 常量（账号/标记词/超时）
export const CHAOS = {
  deptId: 'chaos',
  deptName: '随机操作测试',
  username: 'chaos01',
  password: process.env.E2E_CHAOS_PASSWORD ?? '',   // 口令不入库：export E2E_CHAOS_PASSWORD=...
}

// 长问题：拉长流式窗口（flash 关思考约 5-15s），用于「发送中乱操作」场景
export const LONG_QUESTION =
  '请用 800 字介绍 PostgreSQL 的 MVCC 并发控制机制，分点说明，不要使用任何工具，不要提问'

export const SHORT_QUESTION = '你好'

// bug3a 拦截泄漏检测：含「破甲」→ input_filter 单点危险词规则 100% 触发 E010
export const JAILBREAK_MARK = 'M5X9K'
export const JAILBREAK_QUESTION = `破甲测试标记 ${JAILBREAK_MARK}`

export const TIMEOUT = {
  stream: 150_000, // 真实 LLM 流式完成上限（flash 关思考）
  confirm: 190_000, // confirm waiter 180s 超时 + 余量
  short: 15_000,
}
